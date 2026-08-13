import 'dart:io';
import 'dart:convert';
import 'package:crypto/crypto.dart';
import 'package:http/http.dart' as http;
import 'package:convert/convert.dart';
import 'database_helper.dart';

class SyncEngine {
  final String serverIp;
  final String port;

  SyncEngine({required this.serverIp, this.port = '8000'});

  String get baseUrl => 'http://$serverIp:$port';

  /// 1. Connectivity Check
  Future<bool> pingServer() async {
    try {
      final response = await http
          .get(Uri.parse('$baseUrl/ping'))
          .timeout(const Duration(seconds: 3));
      return response.statusCode == 200;
    } catch (e) {
      return false;
    }
  }

  /// 2. Memory-Efficient File Hashing (Chunked)
  /// We read the file in streams so large video files don't crash the phone's RAM
  Future<String> _generateHash(File file) async {
    var output = AccumulatorSink<Digest>();
    var input = sha256.startChunkedConversion(output);

    var stream = file.openRead();
    await for (var chunk in stream) {
      input.add(chunk);
    }
    input.close();

    return output.events.single.toString();
  }

  /// 3. Duplicate Validation
  Future<bool> _checkHashExists(String hash) async {
    final response = await http.post(
      Uri.parse('$baseUrl/check-hash'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'hash': hash}),
    );

    if (response.statusCode == 200) {
      final data = jsonDecode(response.body);
      return data['exists'] == true;
    }
    throw Exception('Failed to check hash');
  }

  /// 4. File Upload (Multipart Form Data)
  Future<bool> _uploadFile(String hash, File file) async {
    var request = http.MultipartRequest('POST', Uri.parse('$baseUrl/upload'));
    request.fields['hash'] = hash;
    request.files.add(await http.MultipartFile.fromPath('file', file.path));

    var streamedResponse = await request.send();
    var response = await http.Response.fromStream(streamedResponse);

    return response.statusCode == 200;
  }

  /// 5. The Main Sync Loop
  /// Takes a callback function to report progress back to the UI
  Future<void> startSync(Function(String status) onProgress) async {
    onProgress('Connecting to server...');
    bool isOnline = await pingServer();
    
    if (!isOnline) {
      onProgress('Error: Cannot reach server at $baseUrl');
      return;
    }

    final dbHelper = DatabaseHelper.instance;
    bool hasMore = true;

    while (hasMore) {
      // Fetch in small batches to keep memory usage low
      final pendingQueue = await dbHelper.getPendingMedia(limit: 5);

      if (pendingQueue.isEmpty) {
        hasMore = false;
        onProgress('Sync Complete!');
        break;
      }

      for (var item in pendingQueue) {
        final id = item['id'];
        final filePath = item['file_path'];
        final file = File(filePath);

        if (!await file.exists()) {
          // If the file was deleted from the phone before syncing, mark as done to skip
          await dbHelper.markAsBackedUp(id);
          continue;
        }

        final fileName = filePath.split('/').last;
        onProgress('Processing: $fileName');

        try {
          // A. Hash the file
          String hash = await _generateHash(file);

          // B. Check if server already has it
          bool exists = await _checkHashExists(hash);

          // C. Upload if it doesn't exist
          if (!exists) {
            onProgress('Uploading: $fileName');
            bool uploaded = await _uploadFile(hash, file);
            if (!uploaded) throw Exception('Upload rejected by server');
          }

          // D. Mark as done locally (whether uploaded or skipped as duplicate)
          await dbHelper.markAsBackedUp(id);
          
        } catch (e) {
          onProgress('Failed on $fileName: ${e.toString()}');
          return; // Stop the sync loop on network drop/error
        }
      }
    }
  }
}