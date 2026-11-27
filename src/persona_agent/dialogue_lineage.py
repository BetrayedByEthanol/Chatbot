"""Bounded local metadata dependencies, reusing response raw lineage.

Views validate source eligibility; this collector pins actual contributing
local turn IDs even when history trimming drops the original message.
"""
from .raw_context import MAX_RAW_SOURCES


class DialogueDependencies:
    def __init__(self, reserved, historical_fence):
        self.reserved=set(reserved)
        self.source_ids=set()
        self.historical_fence=historical_fence
        self.uses_broad=False

    def admit(self,*rows):
        ids=set().union(*(set(row["source_ids"]) for row in rows))
        broad=any(row["broad_dependency"] for row in rows)
        if broad and self.historical_fence is None:
            return False
        if len(self.reserved | self.source_ids | ids)>MAX_RAW_SOURCES:
            return False
        self.source_ids.update(ids)
        self.uses_broad=self.uses_broad or broad
        return True
