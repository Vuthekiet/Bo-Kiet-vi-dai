const express = require('express');
const path = require('path');
const app = express();
const PORT = process.env.PORT || 3000;

// Phục vụ các file tĩnh trong thư mục
app.use(express.static(__dirname));

// Đọc trực tiếp file Bokietvidai.html làm trang chủ
app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'Bokietvidai.html'));
});

app.listen(PORT, () => {
    console.log(`Server đang chạy tại port ${PORT}`);
});
