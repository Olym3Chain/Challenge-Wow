from enums.leaderboard_period import LEADERBOARD_PERIOD
from helpers.name_helper import generate_funny_username
from repositories.implement.user_repo_impl import UserRepository, UserStatsRepository
from models.leaderboard_entry import LeaderboardEntry
from fastapi import APIRouter, HTTPException, Response

class UserController:
    def __init__(self, user_repo: UserRepository, user_stats_repo: UserStatsRepository):
        self.user_repo = user_repo
        self.user_stats_repo = user_stats_repo

    async def login_or_create(self, wallet_id: str, username: str = None):
        user = await self.user_repo.get_by_wallet(wallet_id)
        if user:
            return user

        if not username:
            username = generate_funny_username(wallet_id)

        return self.user_repo.create(wallet_id, username)

    async def get_by_wallet(self, wallet_id: str):
        user = await self.user_repo.get_by_wallet(wallet_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return user
    
    async def update_username(self, wallet_id: str, username: str):
        return await self.user_repo.update_username(wallet_id, username)

    async def get_leaderboard(self, limit: int = 10, period=LEADERBOARD_PERIOD.ALL_TIME):
        data = await self.user_stats_repo.get_leaderboard(limit, period)
        return [LeaderboardEntry(**item) for item in data]
