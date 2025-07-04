from datetime import datetime, timezone
from typing import List
from fastapi import WebSocket, WebSocketDisconnect
from config.question_config import QUESTION_CONFIG
from enums.player_status import PLAYER_STATUS
from enums.question_difficulty import QUESTION_DIFFICULTY
from helpers.json_helper import send_json_safe
from models.answer import Answer
from models.chat_payload import ChatPayload
from models.kick_player import KickPayload
from models.player import Player
from models.room import Room
from services.answer_service import AnswerService
from services.player_service import PlayerService
from services.question_service import QuestionService
from services.room_service import RoomService
from services.websocket_manager import WebSocketManager
from pydantic import ValidationError
from enums.game_status import GAME_STATUS
from repositories.implement.user_repo_impl import UserRepository, UserStatsRepository
import random, asyncio, pprint, time, uuid
from services.nft_service import BlockchainService
import json
from typing import Dict, List, Optional, Any

class WebSocketController:
    def __init__(self, manager: WebSocketManager, player_service: PlayerService, room_service: RoomService,
                 question_service: QuestionService, answer_service: AnswerService, user_repo: UserRepository, user_stats_repo: UserStatsRepository):
        self.manager = manager
        self.player_service = player_service
        self.room_service = room_service
        self.question_service = question_service
        self.answer_service = answer_service
        self.user_repo = user_repo
        self.user_stats_repo = user_stats_repo
        self.nft_service = BlockchainService()  # Thêm NFT service
        # Thêm tracking cho active tasks
        self.active_tasks = {}

    async def _handle_kick_player(self, websocket: WebSocket, room_id: str, wallet_id: str, data: dict):
        try:
            payload = KickPayload(**data.get("payload", {}))
        except ValidationError:
            return

        host_wallet_id = await self.room_service.get_host_room_wallet(room_id)
        if not host_wallet_id or host_wallet_id != wallet_id:
            await send_json_safe(websocket, {"type": "error", "message": "Only the host can kick players."})
            return

        kicked_ws = await self.manager.get_player_socket_by_wallet(payload.wallet_id)
        if wallet_id == payload.wallet_id:
            await send_json_safe(websocket, {"type": "error", "payload": {"message": "You cannot kick yourself"}})
            return

        result = await self.player_service.leave_room(payload.wallet_id, payload.room_id)

        if kicked_ws:
            await send_json_safe(kicked_ws, {
                "type": "kicked",
                "payload": {"reason": "You were kicked from the room", "roomId": payload.room_id}
            })

        kicked_player: Player = result["data"]
        username = getattr(kicked_player, "username", "Unknown")
        await self.manager.broadcast_to_room(payload.room_id, {
            "type": "player_left",
            "action": "kick",
            "payload": {"walletId": payload.wallet_id, "username": username}
        })

    async def _handle_chat(self, websocket: WebSocket, room_id: str, wallet_id: str, data: dict):
        try:
            payload = ChatPayload(**data.get("payload", {}))
        except ValidationError:
            return

        await self.manager.broadcast_to_room(room_id, {
            "type": "chat",
            "payload": {"sender": payload.sender, "message": payload.message}
        })

    async def _handle_ping(self, websocket: WebSocket, data: dict):
        await send_json_safe(websocket, {"type": "pong"})

    async def _handle_broadcast(self, websocket: WebSocket, data: dict):
        await self.manager.broadcast_to_lobby(data)

    # ✅ Helper function để chuyển sang câu hỏi tiếp theo
    async def _move_to_next_question(self, room_id: str):
        """Chuyển sang câu hỏi tiếp theo"""
        room = await self.room_service.get_room(room_id)
        if not room or room.status != GAME_STATUS.IN_PROGRESS:
            return

        # Tăng index và lưu
        room.current_index += 1
        
        print(f"[DEBUG] Room {room_id} - Current index: {room.current_index}, Total questions: {len(room.current_questions or [])}, Total questions config: {room.total_questions}")
        
        # Kiểm tra xem còn câu hỏi không
        if room.current_index >= room.total_questions:
            # Game kết thúc
            await self._handle_game_end(room_id)
            return
            
        # Fallback: Nếu không có câu hỏi hiện tại nhưng vẫn chưa đạt total_questions, tạo câu hỏi mặc định
        if room.current_index >= len(room.current_questions or []) and room.current_index < room.total_questions:
            print(f"[WARNING] Room {room_id} - No current question but game should continue. Creating fallback question.")
            # Tạo câu hỏi mặc định để game tiếp tục
            fallback_question = await self.question_service.get_random_question()
            room.current_questions = room.current_questions or []
            if fallback_question:
                room.current_questions.append(fallback_question)
            else:
                # Nếu không có câu hỏi nào, kết thúc game
                # print(f"[GAME_END] Room {room_id} - No fallback question available, ending game")
                await self._handle_game_end(room_id)
                return
        
        # Không cần set current_question vì nó là property tự động tính toán
        await self.room_service.save_room(room)

        # Gửi câu hỏi tiếp theo
        await self._send_current_question(room_id)

    # ✅ Handle game end - kết thúc game và tính toán kết quả
    async def _handle_game_end(self, room_id: str):
        """Xử lý khi game kết thúc"""
        room = await self.room_service.get_room(room_id)
        if not room:
            return

        # Cập nhật trạng thái room
        room.status = GAME_STATUS.FINISHED
        game_end_time = datetime.now(timezone.utc)
        room.ended_at = game_end_time
        
        # Tạo results và tính toán điểm số từ answers
        results = []
        for p in room.players:
            # Lấy answers từ service (chú ý thứ tự tham số)
            answers = await self.answer_service.get_answers_by_wallet_id(room_id, p.wallet_id)
            if answers is None:
                answers = []
            
            # Convert Answer objects to dict format for compatibility
            valid_answers = []
            for a in answers:
                if hasattr(a, 'score'):
                    valid_answers.append({
                        "score": a.score,
                        "question_id": a.question_id,
                        "response_time": getattr(a, 'response_time', 0),
                        "is_correct": getattr(a, 'is_correct', a.score > 0)
                    })
                elif isinstance(a, dict) and "score" in a:
                    valid_answers.append(a)
            
            if not valid_answers and p.answers:
                for a in p.answers:
                    valid_answers.append({
                        "score": a.score,
                        "question_id": a.question_id,
                        "response_time": getattr(a, 'response_time', 0),
                        "is_correct": getattr(a, 'is_correct', a.score > 0)
                    })
            
            score = sum(a["score"] for a in valid_answers)
            results.append({
                "wallet": p.wallet_id,
                "oath": p.username,
                "score": score,
                "answers": valid_answers
            })
        
        # Xác định winner
        winner = max(results, key=lambda x: x['score']) if results else None
        winner_wallet = winner['wallet'] if winner else None
        
        # Cập nhật is_winner trực tiếp trên object Player trong RAM
        for p in room.players:
            p.is_winner = (p.wallet_id == winner_wallet)
        
        # Sắp xếp results theo điểm số để tạo leaderboard
        sorted_results = sorted(results, key=lambda x: x['score'], reverse=True)
        
        # Tính toán thống kê game
        total_players = len(room.players)
        total_questions = room.total_questions
        
        # Chuẩn bị leaderboard với ranking chi tiết
        leaderboard = []
        for idx, result in enumerate(sorted_results):
            # Tìm player object để lấy thêm thông tin
            player = next((p for p in room.players if p.wallet_id == result["wallet"]), None)
            
            # Tính toán thống kê chi tiết
            correct_answers = len([a for a in result["answers"] if a.get("score", 0) > 0])
            total_answers = len(result["answers"])
            accuracy = (correct_answers / total_answers * 100) if total_answers > 0 else 0.0
            
            # Tính average response time nếu có dữ liệu
            response_times = [a.get("response_time", 0) for a in result["answers"] if "response_time" in a]
            average_time = sum(response_times) / len(response_times) if response_times else 0.0
            
            player_stats = {
                "rank": idx + 1,
                "walletId": result["wallet"],
                "username": result["oath"],
                "avatar": getattr(player, 'avatar', None) if player else None,
                "score": result["score"],
                "correctAnswers": correct_answers,
                "totalAnswers": total_answers,
                "accuracy": round(accuracy, 2),
                "averageTime": round(average_time, 2),
                "isWinner": result["wallet"] == winner_wallet,
                "reward": 0 
            }
            leaderboard.append(player_stats)

        # Tính toán game statistics
        game_stats = {
            "totalPlayers": total_players,
            "totalQuestions": total_questions,
            "gameMode": getattr(room, 'game_mode', 'standard'),
            "gameDuration": (game_end_time - room.started_at).total_seconds() if room.started_at else 0,
            "averageScore": sum(r['score'] for r in results) / total_players if total_players > 0 else 0,
            "highestScore": sorted_results[0]['score'] if sorted_results else 0,
            "questionBreakdown": {
                "easy": getattr(room, 'easy_questions', 0),
                "medium": getattr(room, 'medium_questions', 0),
                "hard": getattr(room, 'hard_questions', 0)
            }
        }

        # Cập nhật bảng users với thống kê (await vì method là async)
        for result in results:
            await self.user_stats_repo.update_user_stats(
                wallet_id=result["wallet"],
                score=result["score"],
                is_winner=(result["wallet"] == winner_wallet)
            )
        
        # TODO: COULD RECALC EVERY PERIOD
        await self.user_stats_repo.recalculate_ranks()

        # Save updated room
        await self.room_service.save_room(room)

        # Broadcast game end với leaderboard chi tiết
        await self.manager.broadcast_to_room(room_id, {
            "type": "game_ended",
            "payload": {
                "gameStats": game_stats,
                "leaderboard": leaderboard,
                "winner": leaderboard[0] if leaderboard else None,
                "endedAt": int(game_end_time.timestamp() * 1000),
                "roomId": room_id
            },
            # Giữ lại format cũ để tương thích
            "results": [{"wallet": r["wallet"], "oath": r["oath"], "score": r["score"]} for r in results],
            "winner_wallet": winner_wallet
        })

        # Broadcast clear local storage
        await self.manager.broadcast_to_room(room_id, {
            "type": "clear_local_storage"
        })

        # print(f"[GAME_ENDED] Room {room_id} finished. Results: {results}")

        # Mint và transfer NFT cho winner (nếu có)
        if winner_wallet:
            try:
                # Không cần tự tạo metadata_uri nữa, chỉ truyền room_id và winner_wallet
                nft_result = await self._mint_and_transfer_nft(room_id, winner_wallet)
                # print(f"[NFT] Mint and transfer result for room {room_id}: {nft_result}")
                # Broadcast NFT result to room
                await self.manager.broadcast_to_room(room_id, {
                    "type": "nft_awarded",
                    "payload": {
                        "winner_wallet": winner_wallet,
                        "nft_result": nft_result,
                        "room_id": room_id
                    }
                })
            except Exception as e:
                # print(f"[NFT_ERROR] Failed to mint/transfer NFT for room {room_id}: {str(e)}")
                # Broadcast error to room
                await self.manager.broadcast_to_room(room_id, {
                    "type": "nft_error",
                    "payload": {
                        "error": str(e),
                        "room_id": room_id
                    }
                })

        # Schedule room cleanup after some time
        async def cleanup_room():
            await asyncio.sleep(300)  # Wait 5 minutes before cleanup
            await self._cleanup_finished_room(room_id)
        
        asyncio.create_task(cleanup_room())

    # ✅ Update player statistics after game
    async def _update_player_statistics(self, room: Room, leaderboard: list):
        """Cập nhật thống kê của players sau game"""
        try:
            for player_result in leaderboard:
                wallet_id = player_result["walletId"]
                
                # Get current user stats
                user_stats = await self.user_stats_repo.get_user_stats(wallet_id)
                if not user_stats:
                    # Create new stats if not exists
                    user_stats = {
                        "wallet_id": wallet_id,
                        "total_games": 0,
                        "total_wins": 0,
                        "total_score": 0,
                        "total_correct_answers": 0,
                        "total_questions_answered": 0,
                        "average_accuracy": 0.0,
                        "best_score": 0,
                        "current_streak": 0,
                        "best_streak": 0
                    }
                
                # Update stats
                user_stats["total_games"] += 1
                user_stats["total_score"] += player_result["score"]
                user_stats["total_correct_answers"] += player_result["correctAnswers"]
                user_stats["total_questions_answered"] += player_result["totalAnswers"]
                
                if player_result["isWinner"]:
                    user_stats["total_wins"] += 1
                    user_stats["current_streak"] += 1
                    user_stats["best_streak"] = max(user_stats["best_streak"], user_stats["current_streak"])
                else:
                    user_stats["current_streak"] = 0
                
                user_stats["best_score"] = max(user_stats["best_score"], player_result["score"])
                user_stats["average_accuracy"] = user_stats["total_correct_answers"] / user_stats["total_questions_answered"] if user_stats["total_questions_answered"] > 0 else 0.0
                
                # Save updated stats
                await self.user_stats_repo.update_user_stats(wallet_id, user_stats["total_score"], user_stats["total_wins"] > 0)
                
        except Exception as e:
            print(f"Error updating player statistics: {e}")

    # ✅ Cleanup finished room
    async def _cleanup_finished_room(self, room_id: str):
        """Dọn dẹp room đã kết thúc"""
        try:
            # Disconnect all remaining connections
            self.manager.disconnect_room_by_room_id(room_id)
            
            # Optionally remove room from cache/database
            # await self.room_service.delete_room(room_id)
            
        except Exception as e:
            print(f"Error cleaning up room {room_id}: {e}")
    
    async def _handle_start_game(self, websocket: WebSocket, room_id: str, wallet_id: str, data: dict):
        # ✅ 1. Chỉ host mới được bắt đầu game
        host_wallet_id = await self.room_service.get_host_room_wallet(room_id)
        if wallet_id != host_wallet_id:
            await send_json_safe(websocket, {
                "type": "error",
                "message": "Only host can start the game."
            })
            return

        # ✅ 2. Kiểm tra xem game đã bắt đầu chưa
        room = await self.room_service.get_room(room_id)
        if not room:
            await send_json_safe(websocket, {
                "type": "error",
                "message": "Room not found."
            })
            return
            
        if room.status == GAME_STATUS.IN_PROGRESS:
            await send_json_safe(websocket, {
                "type": "error",
                "message": "Game is already in progress."
            })
            return

        easy_count = room.easy_questions if room.easy_questions is not None else QUESTION_CONFIG[QUESTION_DIFFICULTY.EASY]["quantity"]
        medium_count = room.medium_questions if room.medium_questions is not None else QUESTION_CONFIG[QUESTION_DIFFICULTY.MEDIUM]["quantity"]
        hard_count = room.hard_questions if room.hard_questions is not None else QUESTION_CONFIG[QUESTION_DIFFICULTY.HARD]["quantity"]

        # ✅ 4. Lấy danh sách câu hỏi và shuffle
        easy_qs = await self.question_service.get_random_questions_by_difficulty(QUESTION_DIFFICULTY.EASY, easy_count)
        medium_qs = await self.question_service.get_random_questions_by_difficulty(QUESTION_DIFFICULTY.MEDIUM, medium_count)
        hard_qs = await self.question_service.get_random_questions_by_difficulty(QUESTION_DIFFICULTY.HARD, hard_count)

        questions = [*easy_qs, *medium_qs, *hard_qs]
        random.shuffle(questions)

        if not questions:
            await send_json_safe(websocket, {"type": "error", "message": "No questions found."})
            return
            
        # Nếu không đủ câu hỏi, điều chỉnh total_questions
        if len(questions) < (easy_count + medium_count + hard_count):
            pass
            
        # Đảm bảo có đủ câu hỏi bằng cách lặp lại nếu cần
        target_questions = easy_count + medium_count + hard_count
        if len(questions) < target_questions:
            # Lặp lại câu hỏi để đạt được số lượng mong muốn
            repeated_questions = []
            while len(repeated_questions) < target_questions:
                repeated_questions.extend(questions[:target_questions - len(repeated_questions)])
            questions = repeated_questions[:target_questions]

        # ✅ 5. Chuẩn bị dữ liệu câu hỏi cho client (loại bỏ đáp án đúng)
        def prepare_question_for_client(question):
            """Chuẩn bị câu hỏi để gửi cho client, loại bỏ correct_answer"""
            question_dict = question.dict()
            options = question_dict.get("options", [])
            if options:
                shuffled_options = options.copy()
                random.shuffle(shuffled_options)
                question_dict["options"] = shuffled_options
                # Gán lại options đã shuffle vào object gốc để lưu vào room
                question.options = shuffled_options
            question_dict.pop("correct_answer", None)
            return question_dict

        # ✅ 6. Chuẩn bị danh sách câu hỏi cho client
        client_questions = [prepare_question_for_client(q) for q in questions]

        # ✅ 7. Cập nhật trạng thái phòng
        room.players = [p.model_copy(update={"status": PLAYER_STATUS.ACTIVE}) for p in room.players]
        room.status = GAME_STATUS.IN_PROGRESS
        room.current_questions = questions 
        room.current_index = 0
        room.started_at = datetime.now(timezone.utc)

        await self.room_service.save_room(room)
        self.manager.clear_room_timeout(room_id)

        # ✅ 8. Gửi sự kiện bắt đầu game với tất cả câu hỏi (không có đáp án)
        start_at = int(time.time() * 1000) + (room.countdown_duration * 1000)
        await self.manager.broadcast_to_room(room_id, {
            "type": "game_started",
            "payload": {
                "totalQuestions": len(questions),
                "easyCount": easy_count,
                "mediumCount": medium_count,
                "hardCount": hard_count,
                "countdownDuration": room.countdown_duration,
                "startAt": start_at,
                "roomSettings": {
                    "easyQuestions": QUESTION_CONFIG[QUESTION_DIFFICULTY.EASY],
                    "mediumQuestions": QUESTION_CONFIG[QUESTION_DIFFICULTY.MEDIUM],
                    "hardQuestions": QUESTION_CONFIG[QUESTION_DIFFICULTY.HARD],
                }
            }
        })

        # ✅ 9. Gửi câu hỏi đầu tiên sau countdown
        async def send_first_question():
            await asyncio.sleep(room.countdown_duration)
            await self._send_current_question(room_id)

        asyncio.create_task(send_first_question())

    # ✅ Helper function để gửi câu hỏi hiện tại
    async def _send_current_question(self, room_id: str):
        """Gửi câu hỏi hiện tại dựa trên room.current_index"""
        room = await self.room_service.get_room(room_id)
        if not room or room.status != GAME_STATUS.IN_PROGRESS:
            return

        current_question = room.current_question
        if not current_question:
            # Không nên xảy ra vì logic này đã được xử lý trong _move_to_next_question
            # print(f"Warning: No current question for room {room_id}")
            return

        # Lấy config cho câu hỏi hiện tại
        question_config = QUESTION_CONFIG.get(current_question.difficulty)
        if not question_config:
            # Fallback config nếu không tìm thấy
            question_config = QUESTION_CONFIG[QUESTION_DIFFICULTY.EASY]

        # Tính thời gian dựa trên độ khó và setup
        base_time = room.time_per_question
        if current_question.difficulty == QUESTION_DIFFICULTY.EASY:
            time_per_question = base_time
        else:
            # Medium & Hard: base_time + question_config time
            additional_time = question_config["time_per_question"]
            time_per_question = base_time + additional_time
            
        question_start_at = int(time.time() * 1000)
        question_end_at = question_start_at + time_per_question * 1000

        # Chuẩn bị câu hỏi cho client (loại bỏ correct_answer)
        client_question = current_question.model_dump(exclude={"correct_answer"})
        options = client_question.get("options", [])
        if options:
            shuffled_options = options.copy()
            random.shuffle(shuffled_options)
            client_question["options"] = shuffled_options
            # Gán lại options đã shuffle vào current_question và lưu lại vào room
            current_question.options = shuffled_options
            await self.room_service.save_room(room)
        client_question.pop("correct_answer", None)

        await self.manager.broadcast_to_room(room_id, {
            "type": "next_question",
            "payload": {
                "questionIndex": room.current_index,
                "question": client_question,
                "timing": {
                    "questionStartAt": question_start_at,
                    "questionEndAt": question_end_at,
                    "timePerQuestion": time_per_question,                    
                },
                "config": question_config,
                "progress": {
                    "current": room.current_index + 1,
                    "total": room.total_questions
                }
            }
        })
        

        # ✅ FIXED: Only create fallback timer for safety, not for normal progression
        # This timer will only trigger if all players don't answer within time limit
        if room_id not in self.active_tasks:
            async def fallback_auto_next_question():
                try:
                    print(f"[FALLBACK] Room {room_id} - Starting fallback timer for question {room.current_index + 1}")
                    # Wait for the full question time + buffer, then force move to next question
                    await asyncio.sleep(time_per_question + 10)  # 10 seconds buffer
                    
                    # Check if we still need to move (in case normal flow already handled it)
                    current_room = await self.room_service.get_room(room_id)
                    if current_room and current_room.status == GAME_STATUS.IN_PROGRESS and current_room.current_index == room.current_index:
                        print(f"[FALLBACK] Room {room_id} - Auto moving to next question after timeout")
                        
                        # ✅ FIXED: Handle unanswered questions before moving to next question
                        current_question = current_room.current_question
                        if current_question:
                            await self._handle_unanswered_questions(room_id, current_question)
                        
                        # Show question result with all answers (including no answers)
                        await self._show_question_result(room_id, handle_unanswered=False)
                        
                        # ✅ FIXED: The result will automatically trigger next question after 3 seconds
                        # (handled by next_question_delay in _show_question_result)
                    else:
                        print(f"[FALLBACK] Room {room_id} - Skipping fallback (game ended or question already moved)")
                except Exception as e:
                    print(f"[ERROR] Error in fallback_auto_next_question for room {room_id}: {e}")
                finally:
                    # Xóa task khỏi tracking khi hoàn thành
                    self.active_tasks.pop(room_id, None)
                    print(f"[FALLBACK] Room {room_id} - Fallback timer completed")

            task = asyncio.create_task(fallback_auto_next_question())
            self.active_tasks[room_id] = task

    # ✅ Helper function để xử lý submit answer (IMPROVED)
    async def _handle_submit_answer(self, websocket: WebSocket, room_id: str, wallet_id: str, data: dict):
        """Xử lý khi player submit câu trả lời"""
        # print(f"[DEBUG] Received submit_answer data: {data}")
        
        room = await self.room_service.get_room(room_id)
        if not room or room.status != GAME_STATUS.IN_PROGRESS:
            await send_json_safe(websocket, {
                "type": "error",
                "message": "Game is not in progress."
            })
            return

        current_question = room.current_question
        if not current_question:
            await send_json_safe(websocket, {
                "type": "error",
                "message": "No current question available."
            })
            return

        # Lấy config cho câu hỏi hiện tại
        question_config = QUESTION_CONFIG.get(current_question.difficulty)
        if not question_config:
            question_config = QUESTION_CONFIG[QUESTION_DIFFICULTY.EASY]

        # ✅ FIXED: Extract answer from the correct nested structure
        player_answer = data.get("data", {}).get("answer")
        answer = data.get("data", {}).get("answer")
        print(f"[DEBUG] Player answer: '{player_answer}', type: {type(player_answer)}")
        
        # Check if this is a "no answer" submission
        if not player_answer or player_answer == "":
            print(f"[NO_ANSWER] Player {wallet_id} submitted no answer for question {room.current_index + 1}")
        
        print(f"[DEBUG] Current question correct answer: '{current_question.correct_answer}'")
        
        submit_time = int(time.time() * 1000)
        
        # Calculate question start time based on room start time and current question index
        # Tính thời gian dựa trên độ khó và setup
        base_time = room.time_per_question
        if current_question.difficulty == QUESTION_DIFFICULTY.EASY:
            time_per_question = base_time
        else:
            # Medium & Hard: base_time + question_config time
            additional_time = question_config["time_per_question"]
            time_per_question = base_time + additional_time
            
        question_start_at = int(room.started_at.timestamp() * 1000) + room.current_index * time_per_question * 1000 if room.started_at else 0
        
        # Use client-provided timing as fallback if it's reasonable
        client_question_start = data.get("data", {}).get("questionStartAt")
        if client_question_start and abs(client_question_start - question_start_at) < 5000:  # Within 5 seconds
            question_start_at = client_question_start

        # Kiểm tra đáp án đúng (sử dụng correct_answer từ server)
        is_correct = player_answer == current_question.correct_answer
        
        # Tính điểm dựa trên config và thời gian trả lời
        points = 0
        speed_bonus = 0
        time_bonus = 0
        order_bonus = 0
        
        if is_correct:
            base_score = question_config["score"]
            points = base_score
            
            # Tính speed bonus nếu được bật
            if question_config["speed_bonus_enabled"]:
                time_taken = (submit_time - question_start_at) / 1000
                # Sử dụng time_per_question đã tính toán ở trên
                
                # Tính tỷ lệ thời gian còn lại
                time_remaining_ratio = max(0, (time_per_question - time_taken) / time_per_question)
                speed_bonus = int(question_config["max_speed_bonus"] * time_remaining_ratio)
                points += speed_bonus
                
                # ✅ NEW: Time-based bonus - faster answers get more points
                # Tính thời gian nộp sớm (0-100% của thời gian cho phép)
                time_percentage = min(1.0, time_taken / time_per_question)
                # Càng sớm càng nhiều điểm (linear decrease)
                time_bonus = int(question_config["max_speed_bonus"] * 0.5 * (1.0 - time_percentage))
                points += time_bonus
                
                # ✅ NEW: Order bonus - first correct answers get extra points
                try:
                    current_submit_time = datetime.now(timezone.utc)

                    correct_answers = await self.answer_service.get_correct_answers_by_question_id(room_id, current_question.id)
                    if correct_answers:
                        # Tách bước lọc để Pyright không báo lỗi
                        valid_correct_answers: List[Answer] = [a for a in correct_answers if a.submitted_at]

                        correct_answers_sorted = sorted(
                            valid_correct_answers,
                            key=lambda a: a.submitted_at if a.submitted_at else datetime.min
                        )

                        for idx, answer in enumerate(correct_answers_sorted):
                            if answer.wallet_id == wallet_id:
                                if idx == 0:
                                    order_bonus = int(question_config["max_speed_bonus"] * 0.3)
                                elif idx == 1:
                                    order_bonus = int(question_config["max_speed_bonus"] * 0.15)
                                elif idx == 2:
                                    order_bonus = int(question_config["max_speed_bonus"] * 0.05)
                                break

                        points += order_bonus
                except Exception as e:
                    print(f"Error calculating order bonus: {e}")

        print(f"[DEBUG] Points calculated: {points} (base: {question_config['score']}, speed_bonus: {speed_bonus}, time_bonus: {time_bonus}, order_bonus: {order_bonus})")

        # Cập nhật điểm player
        for player in room.players:
            if player.wallet_id == wallet_id:
                player.score += points
                # print(f"[DEBUG] Updated player {wallet_id} score: {player.score}")
                break

        # Lưu answer để tracking (nếu cần)
        try:
            answer_record = Answer(
                room_id=room_id,
                wallet_id=wallet_id,
                score=points,
                question_id=current_question.id if hasattr(current_question, 'id') else "",
                question_index=room.current_index,
                answer=player_answer,
                player_answer=player_answer,
                correct_answer=current_question.correct_answer,
                is_correct=is_correct,
                points_earned=points,
                response_time=submit_time - question_start_at,
                submitted_at=datetime.now(timezone.utc)
            )
            await self.answer_service.save_answer(answer_record)
            # print(f"[DEBUG] Saved answer record: {answer_record.model_dump()}")
        except Exception as e:
            print(f"Error saving answer: {e}")

        # Phản hồi cho player đã submit
        response_time = submit_time - question_start_at
        
        # Determine response message based on answer type
        if not player_answer or player_answer == "":
            response_message = "No answer submitted - 0 points"
        else:
            response_message = f"You earned {points} points"
        
        await send_json_safe(websocket, {
            "type": "answer_submitted",
            "payload": {
                "isCorrect": is_correct,
                "points": points,
                "baseScore": question_config["score"] if is_correct else 0,
                "speedBonus": speed_bonus,
                "timeBonus": time_bonus,
                "orderBonus": order_bonus,
                "correctAnswer": current_question.correct_answer,
                "explanation": getattr(current_question, 'explanation', None),
                "totalScore": next(p.score for p in room.players if p.wallet_id == wallet_id),
                "responseTime": response_time,
                "message": response_message,
                "isNoAnswer": not player_answer or player_answer == ""
            }
        })
        

        await self.room_service.save_room(room)

        # ✅ FIXED: Check if all players have answered this question
        # If all players have answered, show question result and move to next question
        try:
            # Get all answers for current question from database using question_id
            current_question_answers = await self.answer_service.get_answers_by_room_and_question_id(room_id, current_question.id)
            
            # Count how many players have answered this question
            answered_players = set()
            if current_question_answers:
                for answer in current_question_answers:
                    if answer.wallet_id:
                        answered_players.add(answer.wallet_id)
            
            # If all players have answered, show result and move to next question
            if len(answered_players) >= len(room.players):
                # print(f"[DEBUG] All players answered question {room.current_index + 1}, showing result")
                # Cancel any existing auto timer
                if room_id in self.active_tasks:
                    print(f"[DEBUG] Cancelling fallback timer for room {room_id}")
                    self.active_tasks[room_id].cancel()
                    self.active_tasks.pop(room_id, None)
                
                # Show question result immediately
                await self._show_question_result(room_id)
            else:
                print(f"[DEBUG] Waiting for more players to answer question {room.current_index + 1} ({len(answered_players)}/{len(room.players)})")
                
        except Exception as e:
            print(f"Error checking submitted answers: {e}")
            # Fallback: rely on auto timer if there's an error
            pass

    # ✅ Helper function để show kết quả câu hỏi (IMPROVED)
    async def _show_question_result(self, room_id: str, handle_unanswered: bool = True):
        """Hiển thị kết quả của câu hỏi hiện tại"""
        room = await self.room_service.get_room(room_id)
        if not room:
            return

        current_question = room.current_question
        if not current_question:
            return
        
        # ✅ NEW: Handle unanswered questions - automatically submit "no answer" for players who didn't respond
        if handle_unanswered:
            await self._handle_unanswered_questions(room_id, current_question)
        
        # Thống kê câu trả lời từ database
        answer_stats = {}
        for option in current_question.options:
            answer_stats[option] = 0
        
        # Add "No Answer" option to stats
        answer_stats["No Answer"] = 0
        
        # Lấy thống kê từ database
        try:
            current_question_answers = await self.answer_service.get_answers_by_room_and_question_id(room_id, current_question.id)
            if current_question_answers:
                for answer in current_question_answers:
                    if answer.answer and answer.answer in answer_stats:
                        answer_stats[answer.answer] += 1
                    elif not answer.answer or answer.answer == "":
                        answer_stats["No Answer"] += 1
        except Exception as e:
            print(f"Error getting answer stats: {e}")
        
        # Calculate total responses (excluding "No Answer" for percentage calculations)
        total_responses = sum(count for option, count in answer_stats.items() if option != "No Answer")
        
        await self.manager.broadcast_to_room(room_id, {
            "type": "question_result",
            "payload": {
                "questionIndex": room.current_index,
                "correctAnswer": current_question.correct_answer,
                "explanation": getattr(current_question, 'explanation', None),
                "answerStats": answer_stats,
                "totalResponses": total_responses,
                "totalPlayers": len(room.players),
                "options": current_question.options,
                "leaderboard": [
                    {
                        "walletId": p.wallet_id,
                        "username": p.username,
                        "score": p.score,
                        "rank": idx + 1
                    } for idx, p in enumerate(sorted(room.players, key=lambda x: x.score, reverse=True))
                ]
            }
        })

        # ✅ FIXED: Move to next question after showing result
        # Chỉ tạo task nếu chưa có task active cho room này
        if room_id not in self.active_tasks:
            async def next_question_delay():
                try:
                    print(f"[NEXT_QUESTION] Room {room_id} - Waiting 3 seconds before moving to next question")
                    # Wait 3 seconds to show result, then move to next question
                    await asyncio.sleep(3)
                    print(f"[NEXT_QUESTION] Room {room_id} - Moving to next question now")
                    await self._move_to_next_question(room_id)
                except Exception as e:
                    print(f"[ERROR] Error in next_question_delay for room {room_id}: {e}")
                finally:
                    # Xóa task khỏi tracking khi hoàn thành
                    self.active_tasks.pop(room_id, None)
                    print(f"[NEXT_QUESTION] Room {room_id} - Next question delay task completed")
            
            task = asyncio.create_task(next_question_delay())
            self.active_tasks[room_id] = task
            print(f"[NEXT_QUESTION] Room {room_id} - Created next question delay task")
        else:
            print(f"[NEXT_QUESTION] Room {room_id} - Skipping next question delay task (already exists)")

    # ✅ NEW: Handle unanswered questions
    async def _handle_unanswered_questions(self, room_id: str, current_question):
        """Automatically submit 'no answer' for players who didn't respond"""
        room = await self.room_service.get_room(room_id)
        if not room:
            return

        try:
            # Get all answers for current question
            current_question_answers = await self.answer_service.get_answers_by_room_and_question_id(room_id, current_question.id)
            
            # Get list of players who answered
            answered_players = set()
            if current_question_answers:
                for answer in current_question_answers:
                    if answer.wallet_id:
                        answered_players.add(answer.wallet_id)
            
            # Find players who didn't answer
            all_players = set(p.wallet_id for p in room.players)
            unanswered_players = all_players - answered_players
            
            print(f"[UNANSWERED] Room {room_id} - Question {room.current_index + 1}: {len(unanswered_players)} players didn't answer")
            
            # Submit "no answer" for each player who didn't respond
            for wallet_id in unanswered_players:
                try:
                    # Create answer record with no answer (empty string)
                    answer_record = Answer(
                        room_id=room_id,
                        wallet_id=wallet_id,
                        score=0,  # No points for unanswered questions
                        question_id=current_question.id if hasattr(current_question, 'id') else "",
                        question_index=room.current_index,
                        answer="",
                        player_answer="",  # Empty string indicates no answer
                        correct_answer=current_question.correct_answer,
                        is_correct=False,  # No answer is always incorrect
                        points_earned=0,
                        response_time=0,  # No response time for unanswered
                        submitted_at=datetime.now(timezone.utc)
                    )
                    
                    await self.answer_service.save_answer(answer_record)
                    print(f"[UNANSWERED] Auto-submitted no answer for player {wallet_id}")
                    
                except Exception as e:
                    print(f"Error auto-submitting no answer for player {wallet_id}: {e}")
                    
        except Exception as e:
            print(f"Error handling unanswered questions: {e}")

    # Sửa hàm _mint_and_transfer_nft để có logic trực tiếp từ controller
    async def _mint_and_transfer_nft(self, room_id: str, winner_wallet: str) -> dict:
        """Mint NFT cho deployer và transfer cho winner"""
        try:
            # Generate default metadata_uri
            metadata_uri = f"https://challengewave.com/nft/{room_id}"
            
            # Mint NFT trước (nếu chưa có), sau đó transfer cho winner
            try:
                # Thử mint NFT (có thể đã tồn tại)
                mint_result = self.nft_service.mint_nft(room_id, metadata_uri)
                # print(f"[NFT] Mint NFT result: {mint_result}")
            except Exception as mint_error:
                # Nếu NFT đã tồn tại, bỏ qua lỗi và tiếp tục
                if "NFT already exists" in str(mint_error):
                    # print(f"[NFT] NFT already exists for room {room_id}, continuing with transfer...")
                    mint_result = "NFT already existed"
                else:
                    raise mint_error
            
            # Transfer NFT cho winner bằng cách submit game result
            score = 100
            zk_proof = "0x" + "00"*32
            transfer_result = self.nft_service.submit_game_result(room_id, winner_wallet, score, zk_proof)
            
            return {
                "message": "NFT minted and transferred successfully",
                "mint_result": mint_result,
                "transfer_result": transfer_result,
                "winner_wallet": winner_wallet
            }
            
        except Exception as e:
            print(f"[NFT_ERROR] Error in _mint_and_transfer_nft: {str(e)}")
            raise e

    async def force_end_game_for_test(self, room_id: str):
        """Force kết thúc game cho mục đích test NFT"""
        # print(f"[TEST] Force ending game for room {room_id}")
        await self._handle_game_end(room_id)
        return {"message": f"Game force ended for room {room_id}"}

    async def handle_lobby_socket(self, websocket: WebSocket):
        await self.manager.connect_lobby(websocket)
        try:
            while True:
                try:
                    data = await websocket.receive_json()
                except WebSocketDisconnect:
                    break

                msg_type = data.get("type")
                handler = {"ping": self._handle_ping, "broadcast": self._handle_broadcast}.get(msg_type)
                if handler:
                    await handler(websocket, data)
        finally:
            self.manager.disconnect_lobby(websocket)

    async def handle_room_socket(self, websocket: WebSocket, room_id: str, wallet_id: str):
        await self.manager.connect_room(websocket, room_id, wallet_id)

        room = await self.room_service.get_room(room_id)
        if room and room.status == GAME_STATUS.IN_PROGRESS:
            # Chỉ gửi câu hỏi hiện tại nếu có và chưa hết thời gian
            current_question = room.current_question
            if current_question:
                # Kiểm tra xem câu hỏi còn thời gian không
                current_time = int(time.time() * 1000)
                question_config = QUESTION_CONFIG.get(current_question.difficulty, QUESTION_CONFIG[QUESTION_DIFFICULTY.EASY])
                
                # Tính thời gian dựa trên độ khó và setup
                base_time = room.time_per_question
                if current_question.difficulty == QUESTION_DIFFICULTY.EASY:
                    time_per_question = base_time
                else:
                    # Medium & Hard: base_time + question_config time
                    additional_time = question_config["time_per_question"]
                    time_per_question = base_time + additional_time
                
                # Tính thời gian bắt đầu câu hỏi hiện tại
                question_start_time = int(room.started_at.timestamp() * 1000) + room.current_index * time_per_question * 1000 if room.started_at else 0
                question_end_time = question_start_time + time_per_question * 1000
                
                # Chỉ gửi nếu còn thời gian (ví dụ: còn ít nhất 5 giây)
                if current_time < question_end_time - 5000:
                    # Chuẩn bị câu hỏi cho client (loại bỏ correct_answer)
                    client_question = current_question.model_dump(exclude={"correct_answer"})
                    options = client_question.get("options", [])
                    if options:
                        shuffled_options = options.copy()
                        random.shuffle(shuffled_options)
                        client_question["options"] = shuffled_options
                        # Gán lại options đã shuffle vào current_question và lưu lại vào room
                        current_question.options = shuffled_options
                        await self.room_service.save_room(room)
                    client_question.pop("correct_answer", None)
                    
                    await send_json_safe(websocket, {
                        "type": "next_question",
                        "payload": {
                            "questionIndex": room.current_index,
                            "question": client_question,
                            "timing": {
                                "questionStartAt": question_start_time,
                                "questionEndAt": question_end_time,
                                "timePerQuestion": time_per_question,
                            },
                            "config": question_config,
                            "progress": {
                                "current": room.current_index + 1,
                                "total": room.total_questions
                            }
                        }
                    })

        room_handlers = {
            "chat": self._handle_chat,
            "kick_player": self._handle_kick_player,
            "start_game": self._handle_start_game,
            "submit_answer": self._handle_submit_answer,
        }

        try:
            while True:
                try:
                    data = await websocket.receive_json()
                except WebSocketDisconnect:
                    break

                msg_type = data.get("type")
                handler = room_handlers.get(msg_type)
                if handler:
                    await handler(websocket, room_id, wallet_id, data)
                else:
                    print(f"[DEBUG] No handler found for message type: {msg_type}")
        finally:
            self.manager.disconnect_room(websocket, room_id, wallet_id)