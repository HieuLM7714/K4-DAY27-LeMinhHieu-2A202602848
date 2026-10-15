# Reflection (≤1 trang)

**Học viên:** Lê Minh Hiếu — 2A202602848

**Cách tiếp cận:** mỗi sự kiện chỉ gọi đúng một tool có tính phí (tool là tất định nên
gọi lại không thêm thông tin). Lỗi cấu trúc được kiểm tra chính xác: contract
violation, thiếu/thừa upstream, output mồ côi. Mọi tín hiệu số được chuyển thành
z-score có hướng: bắt đầu từ baseline (mean ± 3σ), sau đó siết lại bằng thống kê bền
vững (median/MAD) trên các sự kiện sạch của chính lượt chạy. Vượt ngưỡng công bố luôn
bị cảnh báo.

| Phase | Score | TPR | FPR | Vượt ngân sách |
|---|---|---|---|---|
| Practice | 48.28 | 100% | 5.8% | 0 |
| Public | 46.45 | 100% | 5.8% | 9% (160 sự kiện × 1.5 credit > 220) |
| **Private** | **31.79** | **64.8%** | **2.1%** | **0** |

**Loại lỗi nào khó phát hiện nhất, và vì sao?**

- **Lỗi tinh vi trong private (khó nhất).** Practice/public chủ yếu là lỗi lớn
  (skew 15σ, volume 800 dòng), nên đạt gần tối đa. Private có khoảng 19/54 lỗi nằm
  *bên trong* dải nhiễu bình thường. Ví dụ, tuổi tài liệu embedding 35–38 ngày, trong
  khi batch sạch dao động 13–37 ngày (khoảng 1.3σ). Mỗi sự kiện chỉ có một mẫu, nên
  một lỗi lệch khoảng 1.5σ không thể tách khỏi nhiễu. Em đã thử hạ ngưỡng z từ 1.9
  xuống 1.1: TPR tăng lên 78% nhưng FPR cũng lên 21%, và điểm vẫn chỉ quanh 32. Bắt
  thêm được bao nhiêu lỗi thì cũng báo nhầm gần chừng ấy sự kiện sạch.
- **Lỗi ở giai đoạn warm-up.** Lượt lineage đầu tiên của private đã thiếu
  `raw.customers`, nên không có lịch sử để so sánh. Ngoài ra, `inputs` trong payload
  chỉ khai báo `raw.orders`, nên upstream kỳ vọng phải được học từ các lượt chạy sạch.
- **Vòng phản hồi "nhiễm bẩn" lịch sử.** Lỗi bị bỏ sót lọt vào lịch sử sạch và làm
  phình σ, che các lỗi sau. Ngược lại, nếu loại mọi giá trị đã cảnh báo khỏi lịch sử
  thì phân phối bị cắt cụt, σ co lại và số cảnh báo tăng vọt (FPR 24–39% khi ngưỡng
  thấp). Cách sửa: dùng median/MAD; chỉ học giá trị có z < 2, độc lập với quyết định
  cảnh báo; và giữ σ trong khoảng [0.8, 1.0] × σ của baseline.
- Lưu ý kỹ thuật: `statistics` nằm trong allowlist nhưng tự import `numbers` (bị
  chặn), nên em tự tính median/MAD.

**Nếu làm lại, em sẽ thay đổi gì về đánh đổi chi phí/độ phủ?**

- **Chi phí:** phủ toàn bộ sự kiện là đúng. Private dùng khoảng 300/320 credit,
  không bị phạt. Một lỗi bị bỏ sót tốn khoảng 0.9 điểm, trong khi một lần gọi tool chỉ
  tốn khoảng 0.13 điểm nếu vượt ngân sách.
- **Ngưỡng:** một lần bắt đúng đáng khoảng 4–5 lần cái giá của một lần báo nhầm, nên
  em đặt ngưỡng dưới 3σ (`Z_SINGLE = 1.9`, +0.2 cho mỗi tín hiệu thêm, hiệu chỉnh
  multiple testing). Em chỉ kiểm tra đuôi trên cho các lỗi một chiều (`*_spike`,
  `freshness_lag`, `runtime_anomaly`, `corpus_staleness`). Bài học: em đã hiệu chỉnh
  trên practice/public, nơi lỗi lớn, và trên mô phỏng do mình tự đặt độ lớn lỗi
  (2.5–4σ). Mô phỏng dự đoán khoảng 36 điểm, thực tế private chỉ 31.79, vì lỗi thật
  còn nhỏ hơn.
- **Cải tiến tiếp theo:** với lỗi nằm trong nhiễu, ngưỡng theo từng sự kiện đã chạm
  giới hạn. Cần dùng thông tin *tích lũy qua nhiều sự kiện*: CUSUM/EWMA để phát hiện
  khi phân phối của luồng lệch dần, hoặc mô hình hỗn hợp (mixture) ước lượng
  P(lỗi | x) từ phần mật độ dư so với mô hình sạch. Em cố ý không dùng điểm private
  làm "oracle" để chỉnh ngưỡng theo từng loại sự kiện, vì đó là overfit vào luồng
  chấm điểm.
