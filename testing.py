from pymongo import MongoClient

uri = "mongodb+srv://kireeti:Admin%402026@mitprojcluster.m8z1c.mongodb.net/test?retryWrites=true&w=majority&appName=mitprojcluster"

client = MongoClient(uri)

print(client.list_database_names())