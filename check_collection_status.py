"""Check Milvus collection load status."""

from pymilvus import MilvusClient
from src.core.config import settings

client_kwargs = {"uri": settings.MILVUS_URL}
if settings.MILVUS_TOKEN:
    client_kwargs["token"] = settings.MILVUS_TOKEN

client = MilvusClient(**client_kwargs)

# Check both collections
collections = [settings.MEMORY_COLLECTION_NAME, settings.MEMORY_GROUP_COLLECTION_NAME]

for collection_name in collections:
    print(f"\n{'='*60}")
    print(f"Collection: {collection_name}")
    print(f"{'='*60}")
    
    # Query collection load status
    load_state = client.get_load_state(collection_name=collection_name)
    print(f"Load state: {load_state}")
    
    # Check if collection exists
    exists = client.has_collection(collection_name)
    print(f"Collection exists: {exists}")
    
    # Get collection statistics if it exists
    if exists:
        stats = client.get_collection_stats(collection_name)
        print(f"\nCollection statistics:")
        print(f"  Row count: {stats.get('row_count', 0)}")
        
        # Describe collection schema - full details
        desc = client.describe_collection(collection_name)
        print(f"\nCollection details:")
        print(desc)
    else:
        print(f"Collection '{collection_name}' does not exist")

print(f"\n{'='*60}")
