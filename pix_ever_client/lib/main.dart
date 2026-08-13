import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';
import 'database_helper.dart';
import 'sync_engine.dart';

void main() {
  runApp(const PixEverApp());
}

class PixEverApp extends StatelessWidget {
  const PixEverApp({Key? key}) : super(key: key);

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'PixEver Backup',
      theme: ThemeData(
        primarySwatch: Colors.blueGrey,
        brightness: Brightness.dark,
      ),
      home: const SyncDashboard(),
    );
  }
}

class SyncDashboard extends StatefulWidget {
  const SyncDashboard({Key? key}) : super(key: key);

  @override
  State createState() => _SyncDashboardState();
}

class _SyncDashboardState extends State {
  final TextEditingController _ipController = TextEditingController();
  final dbHelper = DatabaseHelper.instance;
  final ImagePicker _picker = ImagePicker();

  String _statusText = "Ready";
  bool _isSyncing = false;
  bool _isSelecting = false;
  
  int _pendingCount = 0;
  int _backedUpCount = 0;

  @override
  void initState() {
    super.initState();
    _refreshStats();
  }

  Future _refreshStats() async {
    final stats = await dbHelper.getSyncStats();
    setState(() {
      _pendingCount = stats['pending'] ?? 0;
      _backedUpCount = stats['backedUp'] ?? 0;
    });
  }

  /// 1. Opens native gallery to let the user manually pick photos/videos
  Future _selectMedia() async {
    setState(() {
      _isSelecting = true;
      _statusText = "Opening gallery...";
    });

    try {
      // Allows selecting multiple images and videos natively
      final List selectedMedia = await _picker.pickMultipleMedia();

      if (selectedMedia.isEmpty) {
        setState(() {
          _statusText = "No media selected.";
          _isSelecting = false;
        });
        return;
      }

      setState(() => _statusText = "Adding ${selectedMedia.length} items to queue...");

      int addedCount = 0;
      for (var file in selectedMedia) {
        // We use the local file path as the unique ID for the database
        await dbHelper.addMediaToQueue(file.path, file.path);
        addedCount++;
      }
      
      setState(() {
        _statusText = "Added $addedCount items to sync queue.";
        _isSelecting = false;
      });
      
      await _refreshStats();
      
    } catch (e) {
      setState(() {
        _statusText = "Error selecting media: $e";
        _isSelecting = false;
      });
    }
  }

  /// 2. Triggers the upload process
  Future _startSync() async {
    final ip = _ipController.text.trim();
    if (ip.isEmpty) {
      setState(() => _statusText = "Please enter the server IP.");
      return;
    }

    setState(() {
      _isSyncing = true;
    });

    final engine = SyncEngine(serverIp: ip);
    
    await engine.startSync((status) {
      setState(() => _statusText = status);
      _refreshStats();
    });

    setState(() {
      _isSyncing = false;
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('PixEver Dashboard')),
      body: Padding(
        padding: const EdgeInsets.all(20.0),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            TextField(
              controller: _ipController,
              decoration: const InputDecoration(
                labelText: 'Local Server IP (e.g., 192.168.1.15)',
                border: OutlineInputBorder(),
                prefixIcon: Icon(Icons.computer),
              ),
              keyboardType: TextInputType.number,
            ),
            const SizedBox(height: 30),

            Row(
              mainAxisAlignment: MainAxisAlignment.spaceEvenly,
              children: [
                _buildStatCard("Pending", _pendingCount, Colors.orange),
                _buildStatCard("Backed Up", _backedUpCount, Colors.green),
              ],
            ),
            const SizedBox(height: 30),

            Container(
              padding: const EdgeInsets.all(15),
              decoration: BoxDecoration(
                color: Colors.black26,
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: Colors.grey.shade800),
              ),
              child: Text(
                "> $_statusText",
                style: const TextStyle(fontFamily: 'monospace', color: Colors.greenAccent),
              ),
            ),
            const Spacer(),

            ElevatedButton.icon(
              onPressed: _isSelecting || _isSyncing ? null : _selectMedia,
              icon: const Icon(Icons.add_photo_alternate),
              label: const Text("1. Select Media to Backup"),
              style: ElevatedButton.styleFrom(padding: const EdgeInsets.all(15)),
            ),
            const SizedBox(height: 15),
            ElevatedButton.icon(
              onPressed: _isSyncing || _isSelecting || _pendingCount == 0 ? null : _startSync,
              icon: const Icon(Icons.cloud_upload),
              label: const Text("2. Sync Now"),
              style: ElevatedButton.styleFrom(
                padding: const EdgeInsets.all(15),
                backgroundColor: Colors.blueAccent,
                foregroundColor: Colors.white,
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildStatCard(String label, int count, Color color) {
    return Column(
      children: [
        Text(
          count.toString(),
          style: TextStyle(fontSize: 36, fontWeight: FontWeight.bold, color: color),
        ),
        Text(label, style: const TextStyle(fontSize: 16)),
      ],
    );
  }
}