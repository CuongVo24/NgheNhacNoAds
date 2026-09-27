# NgheNhacNoAds

Trình phát nhạc YouTube / YouTube Music không quảng cáo trong terminal, điều khiển từ điện thoại qua SSH.

[English](README.md)

> **Trạng thái:** đang phát triển, ở mốc M0 (thử nghiệm kỹ thuật). Chưa có bản để cài.

## Sẽ làm được gì

- Tìm trên YouTube Music (mặc định) hoặc YouTube và chỉ phát phần âm thanh: không quảng cáo, không cần trình duyệt.
- Chạy dạng TUI toàn màn hình trên laptop.
- Dùng điện thoại Android (Termux, Termius, JuiceSSH) SSH vào để điều khiển. Âm thanh luôn phát ra từ laptop.
- Có playlist riêng, yêu thích, lịch sử nghe, autoplay kiểu radio, lời bài hát chạy theo nhạc, phím media của Windows.
- Làm cho Windows trước, sau đó tới Linux.

## Cách hoạt động

- Một tiến trình nền trên laptop dùng [yt-dlp](https://github.com/yt-dlp/yt-dlp) (dạng thư viện Python) để lấy luồng âm thanh của bài hát, rồi phát bằng [mpv](https://mpv.io/).
- TUI trên laptop nói chuyện với tiến trình nền qua localhost.
- Tiến trình nền có sẵn một SSH server ([asyncssh](https://github.com/ronf/asyncssh)) để phục vụ đúng TUI đó cho điện thoại. Server chỉ nhận kết nối trong mạng LAN, và chỉ nhận khóa đã ghép đôi bằng mã dùng một lần.

## Tài liệu

- [Đặc tả kỹ thuật](docs/SPEC.md)
- [Ghi chép thử nghiệm mốc M0](docs/spikes/README.md)

## Phát triển

Cần [uv](https://docs.astral.sh/uv/) và Python 3.11 trở lên.

```bash
uv sync
uv run pytest
```

## Miễn trừ trách nhiệm

Dự án dùng yt-dlp để truy cập nội dung YouTube ngoài trình phát chính thức, việc này có thể vi phạm Điều khoản dịch vụ của YouTube. Dự án phục vụ mục đích cá nhân và học tập; không tải về, không phân phối lại nội dung. Dự án không liên quan tới YouTube hay Google.

## Giấy phép

[MIT](LICENSE)
