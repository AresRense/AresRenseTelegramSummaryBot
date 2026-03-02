import sqlite3

processed_tags = input() # by ai
processed_main = input() # by ai

with sqlite3.connect ("mainai.db") as db:
    c = db.cursor()

    c.execute("""--sql
    CREATE TABLE IF NOT EXISTS posts (main TEXT, tags TEXT)""")

    c.execute("""--sql 
    INSERT INTO posts VALUES (?, ?), (processed_main, processed_tags)""")