from pymongo import MongoClient


client = MongoClient("mongodb://root:PUTPASSHERE4@covalent.int.kit.edu:27017/")
name = "wolfgang"
# Access the database (creates it if it doesn't exist)
for db_name in [f"{name}_data", f"{name}_test"]:
    # Access the database (creates it if it doesn't exist)
    db = client[db_name]
    # Insert a document to actually create the database
    collection = db["delete_me"]
    collection.insert_one({"name": "First document"})

# The database now exists on the server
print(client.list_database_names())
