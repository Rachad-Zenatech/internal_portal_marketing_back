"""What counts as travel (flights, hotels, ground transport, per diems and other travel), in one place.

Travel counts toward an event's total cost and ROI, never against a marketing budget. A cost is travel when it came
from a travel feed (Navan, or lines left over from the old travel feed, source TRAVEL_FEED), or when its category is
under the Travel & accommodation expense type (travel entered here by hand or from a cost sheet). Used by the budget
views (services/budgets/budget_service.py), the event cards (services/events/event_management_service.py) and the Events Overview
(services/events/event_performance_service.py), so they always agree.
"""

# Sources whose costs are always travel, whatever their category
TRAVEL_SOURCES = ("TRAVEL_FEED", "NAVAN")
# The expense type every travel category sits under
TRAVEL_EXPENSE_TYPE = "TRAVEL_ACCOMMODATION"

_SOURCES_SQL = ", ".join(f"'{source}'" for source in TRAVEL_SOURCES)


def travel_sql(line: str = "s") -> str:
    """A SQL condition that is true when the spend line aliased `line` is travel."""
    return (
        f"({line}.source IN ({_SOURCES_SQL}) OR EXISTS ("
        f"SELECT 1 FROM spend_categories tc JOIN expense_types tt ON tt.id = tc.expense_type_id "
        f"WHERE tc.id = {line}.category_id AND tt.code = '{TRAVEL_EXPENSE_TYPE}'))"
    )
