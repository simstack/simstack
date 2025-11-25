from pymongo import MongoClient
from simstack.util.config_reader import ConfigReader

config = ConfigReader()

# Connect to your MongoDB
client = MongoClient(config.connection_string)  # Adjust connection string if needed
db = client.simstack  # Using the simstack database


# List all collections in the database
print("Collections in the database:", db.list_collection_names())

# Check if NodeRegistry exists
if "node_registry" in db.list_collection_names():
    # Count documents in NodeRegistry
    count = db.node_registry.count_documents({})
    print(f"Found {count} documents in NodeRegistry collection")


# Update all documents in the NodeRegistry collection
result = db.node_registry.update_many(
    {}, {"$set": {"parameters_id": "67ed96b6cac2bb8d6b528176"}}
)

print(f"Modified {result.modified_count} documents")
