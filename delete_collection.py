"""Script to drop the memories collection."""

from pymilvus import MilvusClient
from src.core.config import settings

def drop_memories_collection():
    """Drop the memories collection."""
    client_kwargs = {"uri": settings.MILVUS_URL}
    if settings.MILVUS_TOKEN:
        client_kwargs["token"] = settings.MILVUS_TOKEN

    client = MilvusClient(**client_kwargs)
    collection_name = settings.MEMORY_COLLECTION_NAME
    
    if client.has_collection(collection_name):
        print(f"Dropping collection '{collection_name}'...")
        client.drop_collection(collection_name)
        print(f"Collection '{collection_name}' dropped successfully.")
    else:
        print(f"Collection '{collection_name}' does not exist.")
    
    client.close()

if __name__ == "__main__":
    drop_memories_collection()
