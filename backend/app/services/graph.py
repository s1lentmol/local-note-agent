"""Read the persisted semantic graph without model calls."""

from app.dto import GraphQuery, GraphResponse
from app.postgres import Database
from app.repositories.graph import GraphRepository


class GraphService:
    def __init__(self, database: Database):
        self.database = database

    def get(self, query: GraphQuery) -> GraphResponse:
        with self.database.connection() as conn:
            return GraphRepository.read(conn, GraphQuery.model_validate(query))
