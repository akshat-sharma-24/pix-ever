import 'package:sqflite/sqflite.dart';
import 'package:path/path.dart';

class DatabaseHelper {
  static final DatabaseHelper instance = DatabaseHelper._init();
  static Database? _database;

  DatabaseHelper._init();

  Future get database async {
    if (_database != null) return _database!;
    _database = await _initDB('sync_queue.db');
    return _database!;
  }

  Future _initDB(String filePath) async {
    final dbPath = await getDatabasesPath();
    final path = join(dbPath, filePath);
    return await openDatabase(path, version: 1, onCreate: _createDB);
  }

  Future _createDB(Database db, int version) async {
    await db.execute('''
      CREATE TABLE media (
        id TEXT PRIMARY KEY,
        file_path TEXT NOT NULL,
        status TEXT NOT NULL
      )
    ''');
  }

  Future addMediaToQueue(String id, String filePath) async {
    final db = await instance.database;
    await db.insert(
      'media',
      {'id': id, 'file_path': filePath, 'status': 'PENDING'},
      conflictAlgorithm: ConflictAlgorithm.ignore,
    );
  }

  Future<List<Map<String, dynamic>>> getPendingMedia({int limit = 10}) async {
    final db = await instance.database;
    return await db.query(
      'media',
      where: 'status = ?',
      whereArgs: ['PENDING'],
      limit: limit,
    );
  }

  Future markAsBackedUp(String id) async {
    final db = await instance.database;
    await db.update(
      'media',
      {'status': 'BACKED_UP'},
      where: 'id = ?',
      whereArgs: [id],
    );
  }

  Future<Map<String, dynamic>> getSyncStats() async {
    final db = await instance.database;
    
    final pendingCount = Sqflite.firstIntValue(
      await db.rawQuery("SELECT COUNT(*) FROM media WHERE status = 'PENDING'")
    ) ?? 0;
    
    final backedUpCount = Sqflite.firstIntValue(
      await db.rawQuery("SELECT COUNT(*) FROM media WHERE status = 'BACKED_UP'")
    ) ?? 0;

    return {
      'pending': pendingCount,
      'backedUp': backedUpCount,
      'total': pendingCount + backedUpCount,
    };
  }
}