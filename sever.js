const express = require('express');
const path = require('path');
const app = express();
const PORT = process.env.PORT || 3000;

app.use(express.static(__dirname));

app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'Bokietvidai.html'));
});

app.listen(PORT, () => console.log(`Server chạy tại port ${PORT}`));
