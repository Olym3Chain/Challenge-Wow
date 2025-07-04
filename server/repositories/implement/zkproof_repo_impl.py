from typing import List
from config.database import supabase
from repositories.interfaces.zkproof_repo import IZkProofRepository
import uuid
from datetime import datetime, timezone
import json


class ZkProofRepository(IZkProofRepository):
    table = "game_results"

    def save_proof(
        self, room_id: str, winner_wallet_id: str, proof: str, scores: List[dict]
    ) -> None:
        data = {
            "id": str(uuid.uuid4()),
            "room_id": room_id,
            "winner_wallet_id": winner_wallet_id,
            "proof": proof,
            "scores": json.dumps(scores),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

        supabase.table(ZkProofRepository.table).insert(data).execute()
