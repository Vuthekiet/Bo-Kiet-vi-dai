const express = require('express');
const path = require('path');
const app = express();
const PORT = process.env.PORT || 3000;

// Phục vụ tất cả các file trong thư mục
app.use(express.static(__dirname));

// Trả về file giao diện index.html khi vào trang chủ
app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'index.html'));
});

app.listen(PORT, () => {
    console.log(`Server Trái Tim 3D đang chạy tại port ${PORT}`);
});
