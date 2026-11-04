"""Narrative Memory Manager - Handles narrative memory grouping operations.

This module implements the logic to group episodic memories into narrative
groups based on vector similarity and usage patterns.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import numpy as np

from ..config import MemoryConfig
from ..utils import build_filter, normalize

if TYPE_CHECKING:
    from ..clients.milvus_store import MilvusStore

logger = logging.getLogger(__name__)


class NarrativeMemoryManager:
    """Manager for narrative memory grouping operations.
    
    This class handles logic of grouping episodic memories into
    narrative groups based on vector similarity.
    
    Uses MilvusStore's public API for all database operations.
    """
    
    def __init__(self, milvus_store: MilvusStore, config: MemoryConfig):
        """Initialize narrative memory manager.
        
        Args:
            milvus_store: Milvus store instance
            config: Memory system configuration
        """
        self._store = milvus_store
        self._config = config

    def assign_to_narrative_group(
        self,
        memory_ids: list[int],
        user_id: str,
        character_id: str
    ) -> dict[int, int]:


        """将被使用的情景记忆分配到叙事组。
        
        Args:
            memory_ids: 被MemoryUsageJudge判断为实际使用的情景记忆ID列表
            user_id: 用户标识
            
        Returns:
            Dict[int, int] - memory_id到group_id的映射
        """
        if not memory_ids:
            return {}
        
        results = {}
        created_groups = 0
        reused_groups = 0
        failed_ids = []
        touched_existing_groups: set[int] = set()
        
        # Ensure groups collection exists
        self._store.create_groups_collection(dim=self._config.embedding_dim)

        # Batch fetch candidate memories once to avoid per-id query overhead.
        candidate_ids: list[int] = []
        for mid in memory_ids:
            try:
                candidate_ids.append(int(mid))
            except Exception:
                failed_ids.append(mid)
                logger.warning("Invalid memory id in input: %s", mid)
        candidate_ids = sorted(set(candidate_ids))
        if not candidate_ids:
            return {}
        candidate_res = self._store.query(
            filter_expr=build_filter(
                user_id,
                character_id,
                f"id in {candidate_ids}",
            ),
            output_fields=["id", "group_id", "vector"],
        )
        id_to_memory = {
            int(row["id"]): row
            for row in candidate_res
            if row.get("id") is not None
        }
        
        for memory_id in memory_ids:
            try:
                memory_id = int(memory_id)
                row = id_to_memory.get(memory_id)
                if not row:
                    logger.warning(f"Memory {memory_id} not found, skipping")
                    continue
                
                current_group_id = int(row.get("group_id", -1))
                vector = row.get("vector")
                if vector is None:
                    logger.warning(f"Memory {memory_id} has no vector, skipping")
                    failed_ids.append(memory_id)
                    continue

                v_mem = np.array(vector)
                v_mem = normalize(v_mem)
                
                # 如果已经分组，跳过
                if current_group_id != -1:
                    logger.debug(f"Memory {memory_id} already in group {current_group_id}")
                    results[memory_id] = current_group_id
                    continue
                
                # 步骤2：在groups上做ANN搜索，找最相似组
                group_hits = self._store.search_groups(
                    user_id=user_id,
                    character_id=character_id,
                    vector=v_mem.tolist(),
                    limit=1
                )

                
                best_group = group_hits[0] if group_hits else None
                
                # 步骤3：阈值判断：新建组 or 加入已有组
                threshold = self._config.narrative_similarity_threshold
                
                if best_group is None or best_group["sim"] < threshold:
                    # 步骤3.1：新建组
                    group_id = self._store.insert_group(
                        user_id=user_id,
                        character_id=character_id,
                        centroid_vector=v_mem.tolist(),
                        size=1
                    )

                    
                    if group_id is None:
                        logger.error(f"Failed to create group for memory {memory_id}")
                        failed_ids.append(memory_id)
                        continue
                    
                    # 更新memory的group_id
                    updated = self._store.update_memory_group_id(
                        memory_id,
                        group_id,
                        user_id,
                        character_id
                    )
                    if not updated:
                        logger.error(f"Failed to update memory {memory_id} group_id to {group_id}")
                        failed_ids.append(memory_id)
                        continue

                    
                    logger.info(f"Created new group {group_id} for memory {memory_id}")
                    results[memory_id] = group_id
                    created_groups += 1
                    
                else:
                    # 步骤3.2：加入已有组（重算中心）
                    group_id = best_group["group_id"]
                    
                    # 防止 group_id 为 None 导致无效查询表达式
                    if group_id is None:
                        logger.error(f"Invalid group_id (None) from search_groups for memory {memory_id}")
                        failed_ids.append(memory_id)
                        continue
                    group_id = int(group_id)
                    
                    # 1) 更新memories.group_id
                    updated = self._store.update_memory_group_id(
                        memory_id,
                        group_id,
                        user_id,
                        character_id
                    )
                    if not updated:
                        logger.error(f"Failed to update memory {memory_id} group_id to {group_id}")
                        failed_ids.append(memory_id)
                        continue
                    
                    touched_existing_groups.add(group_id)
                    logger.info(
                        "Added memory %s to existing group %s (centroid recompute deferred)",
                        memory_id,
                        group_id,
                    )
                    results[memory_id] = group_id
                    reused_groups += 1
                    
            except Exception as e:
                logger.error(f"Failed to assign memory {memory_id} to narrative group: {e}")
                failed_ids.append(memory_id)
                continue

        # Recompute each touched existing group only once per batch.
        for group_id in sorted(touched_existing_groups):
            try:
                members_res = self._store.query(
                    filter_expr=build_filter(
                        user_id,
                        character_id,
                        f"group_id == {group_id}"
                    ),
                    output_fields=["id", "vector"],
                )
                vectors = [
                    row["vector"]
                    for row in members_res
                    if row.get("vector") is not None
                ]
                size = len(members_res)
                if vectors:
                    new_centroid = normalize(np.mean(np.array(vectors), axis=0))
                    self._store.update_group(
                        user_id=user_id,
                        character_id=character_id,
                        group_id=group_id,
                        centroid_vector=new_centroid.tolist(),
                        size=size
                    )
                    logger.info(f"Updated touched group {group_id} centroid (size: {size})")
                else:
                    logger.warning(
                        "Touched group %s has no vectors for centroid update (size=%s)",
                        group_id,
                        size,
                    )
            except Exception as e:
                logger.error(f"Failed to recompute touched group {group_id}: {e}")

        logger.info(
            "Narrative group assignment completed: assigned=%s created=%s reused=%s failed=%s",
            len(results),
            created_groups,
            reused_groups,
            len(failed_ids),
        )
        
        return results

    def cleanup_group(
        self,
        *,
        user_id: str,
        character_id: str,
        group_id: int,
        exclude_memory_id: int | None = None,
    ) -> dict[str, bool]:
        """Recompute or delete a narrative group after membership changes."""
        group_deleted = False
        group_updated = False

        if group_id == -1:
            return {"group_deleted": group_deleted, "group_updated": group_updated}

        members_res = self._store.query(
            filter_expr=build_filter(
                user_id,
                character_id,
                f"group_id == {group_id}",
            ),
            output_fields=["id", "vector"],
        )

        remaining_members = members_res
        if exclude_memory_id is not None:
            remaining_members = [
                row for row in members_res if row.get("id") != exclude_memory_id
            ]

        n = len(remaining_members)
        if n == 0:
            self._store.delete_group(user_id, character_id, group_id)
            logger.info(f"Deleted empty group {group_id}")
            group_deleted = True
            return {"group_deleted": group_deleted, "group_updated": group_updated}

        vectors = [
            row["vector"]
            for row in remaining_members
            if row.get("vector") is not None
        ]
        if vectors:
            new_centroid = normalize(np.mean(np.array(vectors), axis=0))
            self._store.update_group(
                user_id=user_id,
                character_id=character_id,
                group_id=group_id,
                centroid_vector=new_centroid.tolist(),
                size=n,
            )
            logger.info(f"Updated group {group_id} centroid (size: {n})")
            group_updated = True
        else:
            logger.warning(
                "Group %s has members=%s but no vectors available for centroid update",
                group_id,
                n,
            )

        return {"group_deleted": group_deleted, "group_updated": group_updated}
    
    def delete_memory_from_group(
        self,
        memory_id: int,
        user_id: str,
        character_id: str
    ) -> None:

        """删除记忆时同步更新叙事组。
        
        Args:
            memory_id: 要删除的记忆ID
            user_id: 用户标识
        """
        try:
            # 步骤1：查出group_id
            res = self._store.query(
                filter_expr=build_filter(
                    user_id,
                    character_id,
                    f"id == {memory_id}"
                ),
                output_fields=["group_id"],
            )

            
            if not res:
                logger.warning(f"Memory {memory_id} not found for group cleanup")
                return
            
            group_id = res[0]["group_id"]
            group_deleted = False
            group_updated = False
            
            # 步骤2：如有必要，更新或删除组
            if group_id != -1:
                cleanup = self.cleanup_group(
                    user_id=user_id,
                    character_id=character_id,
                    group_id=group_id,
                    exclude_memory_id=memory_id,
                )
                group_deleted = cleanup.get("group_deleted", False)
                group_updated = cleanup.get("group_updated", False)
            
            logger.info(
                "Narrative group cleanup completed: memory_id=%s group_id=%s deleted=%s updated=%s",
                memory_id,
                group_id,
                group_deleted,
                group_updated,
            )
                    
        except Exception as e:
            logger.error(f"Failed to cleanup group for memory {memory_id}: {e}")

    def get_group_members(
        self,
        group_id: int,
        user_id: str,
        character_id: str
    ) -> list[dict[str, Any]]:

        """获取叙事组中的所有成员。
        
        Args:
            group_id: 叙事组ID
            user_id: 用户标识
            
        Returns:
            组内所有记忆列表
        """
        try:
            members_res = self._store.query(
                filter_expr=build_filter(
                    user_id,
                    character_id,
                    f"group_id == {group_id}"
                ),
                output_fields=[
                    "id",
                    "user_id",
                    "character_id",
                    "memory_type",
                    "ts",
                    "chat_id",
                    "text",
                    "group_id",
                ],
            )

            
            logger.info(
                "Narrative group members loaded: group_id=%s members=%s",
                group_id,
                len(members_res),
            )
            return members_res
            
        except Exception as e:
            logger.error(f"Failed to get members for group {group_id}: {e}")
            return []
