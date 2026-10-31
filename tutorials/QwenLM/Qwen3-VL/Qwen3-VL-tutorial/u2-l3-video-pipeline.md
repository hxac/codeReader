# 视频链路：fetch_video、smart_nframes 与三种解码后端

## 1. 本讲目标

上一讲（u2-l2）我们搞清楚了「一张图如何被压成视觉 token」。这一讲把时间维度加进来，学完本讲你应该能够：

1. **手算采样帧数**：给定视频总帧数、原始 fps 和期望 fps，不运行代码也能算出 `smart_nframes` 的返回值。
2. **讲清三种解码后端**（torchvision / decord / torchcodec）的各自实现、自动选择优先级、强制切换方式与失败回退逻辑。
3. **理解视频预算的两层结构**：`total_pixels` 如何与 `min_pixels` / `max_pixels` 联动，共同决定「单帧分辨率」，以及为什么它与帧数无关。
4. **使用帧列表输入路径**：没有视频文件、只有一组图片时，如何让它们被当作视频处理，并得到一份「合成的」视频元数据。

本讲全部源码集中在一个文件里：`qwen-vl-utils/src/qwen_vl_utils/vision_process.py`。

## 2. 前置知识

### 2.1 视频 = 一摞帧

视频文件（如 mp4）本质是**按固定频率拍摄的一摞图片**，再经过编码压缩（如 H.264）得到的产物。三个基本名词：

- **帧（frame）**：视频中的单张图片。一个 10 秒、每秒 30 帧的视频共有 300 帧。
- **fps（frames per second）**：每秒帧数，即拍摄节奏。上面例子里原始 fps 是 30。
- **解码器（decoder）**：把压缩后的视频文件还原成一摞帧像素的程序。解码是 CPU 密集操作，长视频解码往往比模型推理的前处理更耗时。

模型不可能吃下全部 300 帧——那相当于一次输入 300 张图。所以在送入模型前，必须在**时间维度均匀采样**出少量帧，再在**空间维度压缩每帧分辨率**。本讲讲的正是这两件事的源码实现。

### 2.2 回顾：视觉 token 与 factor

承接 u2-l1 / u2-l2 的结论：

- Qwen3-VL 的 `image_patch_size=16`，乘上 `SPATIAL_MERGE_SIZE=2` 得到对齐因子 \( factor = 32 \)，即 resize 后的宽高都必须是 32 的倍数。
- 每帧图像被切成 \( 32\times32 \) 的 patch 再 2×2 合并，因此**每帧的视觉 token 数 ≈ (H/32) × (W/32)**。
- `fetch_video` 的 `image_patch_size` 参数**默认是 14**（为 Qwen2.5-VL 准备的），Qwen3-VL 用户必须显式传 `image_patch_size=16`——这是 u2-l1 反复强调的坑，本讲所有示例代码都会显式传入。

### 2.3 时间 2× 压缩：为什么处处是 2 的倍数

Qwen-VL 系列对视频不仅做空间压缩，还做**时间压缩**：相邻两帧被组成一组，产生一个时间步的 token。这就是源码里的常量 `FRAME_FACTOR = 2`。它带来两个硬约束：

- 采样帧数必须是**偶数**（否则凑不成对）；
- 视频总视觉 token 数 ≈ **(帧数 / 2) × 每帧 token 数**。

记住这个 2，本讲后面几乎所有公式里都藏着它。

### 2.4 一个容易踩的坑：环境变量在 import 时读取

`FORCE_QWENVL_VIDEO_READER`（强制指定解码后端的环境变量）在**模块被 import 的那一刻**就被读取，且选择函数带 `@lru_cache` 缓存。这意味着：在同一个 Python 进程里修改 `os.environ` 再调用是**无效的**，切换后端必须每个后端启动一个新进程。4.2.4 的实践会用到这一点。

## 3. 本讲源码地图

| 位置 | 名称 | 作用 |
|---|---|---|
| vision_process.py L28-L37 | `VIDEO_MIN/MAX_TOKEN_NUM` 等常量 | 视频默认预算与采样参数 |
| vision_process.py L144-L181 | `smart_nframes` | 计算「采多少帧」，本讲核心之一 |
| vision_process.py L184-L225 | `_read_video_torchvision` | 后端一：torchvision 全量解码 |
| vision_process.py L234-L289 | `calculate_video_frame_range` | 由 video_start/video_end 换算起止帧号 |
| vision_process.py L292-L329 | `_read_video_decord` | 后端二：decord 随机访问解码 |
| vision_process.py L338-L377 | `_read_video_torchcodec` | 后端三：torchcodec 解码 |
| vision_process.py L380-L400 | 后端注册表与选择器 | 优先级、强制切换、缓存 |
| vision_process.py L403-L480 | `fetch_video` | 主函数：串起解码、预算、resize，本讲核心之二 |
| README.md（根目录）L551-L611 | 视频输入写法与预算建议 | 官方文档口径 |

永久链接统一前缀为当前 HEAD `9658872`，下文不再重复说明。

## 4. 核心概念与源码讲解

### 4.1 smart_nframes：采样多少帧由谁决定

#### 4.1.1 概念说明

`smart_nframes` 回答的问题是：**「从 total_frames 帧里均匀抽出多少帧送给模型？」**

用户有两种互斥的指定方式（写在视频元素 dict 里）：

- `nframes`：直接指定「就要 N 帧」；
- `fps`：指定「按每秒 F 帧的节奏采」（默认 2.0），并可搭配 `min_frames` / `max_frames` 设上下限。

无论哪种方式，最终结果都会被**对齐到偶数**（时间 2× 压缩的要求），并被限制在 \([2, total\_frames]\) 区间内。

#### 4.1.2 核心流程

`nframes` 分支很简单：\( nframes = \mathrm{round}(N / 2) \times 2 \)。

`fps` 分支是本模块的重点，可以写成一条公式链：

\[
\begin{aligned}
n_{raw} &= \frac{total\_frames}{video\_fps} \times fps \\
n_{clamped} &= \min\Big(\min\big(\max(n_{raw},\ min\_frames),\ max\_frames\big),\ total\_frames\Big) \\
nframes &= \lfloor n_{clamped} / 2 \rfloor \times 2
\end{aligned}
\]

其中默认值：\( fps=2.0 \)，\( min\_frames = \lceil 4/2 \rceil \times 2 = 4 \)，\( max\_frames = \lfloor \min(768, total\_frames)/2 \rfloor \times 2 \)。

直译成文字：**先按「时长 × 期望 fps」算理想帧数，再夹进 [4, max_frames] 与 [·, total_frames] 的双重区间，最后向下取偶**。

手算示例（video_fps=30 的视频）：

| 时长 | total_frames | 期望 fps | 理想帧数 \(n_{raw}\) | 夹逼后 | 最终 nframes |
|---|---|---|---|---|---|
| 10 s | 300 | 2.0（默认） | 300/30×2 = 20 | 20 | **20** |
| 10 s | 300 | 1.0 | 10 | 10 | **10** |
| 10 s | 300 | 4.0 | 40 | 40 | **40** |
| 2 s | 60 | 2.0 | 4 | 4 | **4** |
| 0.5 s | 15 | 2.0 | 1 | max(1, 4) = 4 | **4**（触发下限） |

注意最后一行：短视频理想帧数低于 `min_frames=4` 时，会被下限强行抬到 4 帧。

#### 4.1.3 源码精读

先看采样相关常量：

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L31-L37](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L31-L37)

这 6 行定义了默认采样节奏：`FPS=2.0`（默认每秒采 2 帧）、`FRAME_FACTOR=2`（时间 2× 压缩）、`FPS_MIN_FRAMES=4` / `FPS_MAX_FRAMES=768`（fps 模式下的帧数上下限），以及并行抽帧的最大线程数 8。`MODEL_SEQ_LEN`（默认 128000，可用环境变量覆盖）留到 4.3 讲。

然后是 `smart_nframes` 本体：

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L167-L181](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L167-L181)

逐行解读这段代码：

- L167：断言 `fps` 与 `nframes` 不能同时出现，二选一；
- L168-169：`nframes` 分支——`round_by_factor(N, 2)` 把用户给的帧数四舍五入到最近的偶数；
- L171-173：`fps` 分支的三个默认值，注意 `min_frames` 用**向上**取整、`max_frames` 用**向下**取整（保证区间不被取整撑破），且 `max_frames` 不超过视频实际总帧数；
- L174：核心公式 \( n_{raw} = total\_frames / video\_fps \times fps \)，即「时长 × 期望 fps」；
- L175-176：理想帧数超过总帧数时打 warning（说明期望 fps 比原始 fps 还高，采不出那么多）；
- L177：三重夹逼 \( \min(\min(\max(n_{raw}, min\_frames), max\_frames), total\_frames) \)；
- L178：`floor_by_factor(..., 2)` 向下对齐到偶数；
- L179-180：最终校验 \( 2 \le nframes \le total\_frames \)，不满足直接抛 `ValueError`（例如 total_frames 本身只有 1 帧时无解）。

#### 4.1.4 代码实践

**实践目标**：不依赖任何视频文件，用假数据验证你会手算 `smart_nframes`。

1. 操作步骤：把下面的脚本保存为 `check_nframes.py`（**示例代码**，非项目原有文件）并运行（需先 `pip install -e ./qwen-vl-utils`）：

```python
# 示例代码：check_nframes.py
from qwen_vl_utils.vision_process import smart_nframes

CASES = [
    # (total_frames, video_fps, 采样配置)
    (300, 30, {}),            # 10s，默认 fps=2
    (300, 30, {"fps": 1.0}),
    (300, 30, {"fps": 4.0}),
    (60,  30, {"fps": 2.0}),  # 2s
    (15,  30, {"fps": 2.0}),  # 0.5s，预期触发 min_frames
    (300, 30, {"nframes": 7}),  # nframes 分支，7 会被 round 到 8
]

for total, vfps, ele in CASES:
    print(f"total={total:>3}, video_fps={vfps}, {ele} -> {smart_nframes(ele, total_frames=total, video_fps=vfps)}")
```

2. 需要观察的现象：每行输出是否与你按 4.1.2 公式手算的结果一致。
3. 预期结果（待本地验证）：`20, 10, 40, 4, 4, 8`。最后一行尤其值得注意：`round_by_factor(7, 2)` 中 Python 的 `round(3.5)` 是银行家舍入，结果为 4，于是得到 8。
4. 若某行不一致，回到 4.1.2 的公式链逐步核对是哪一步夹逼出了偏差。

#### 4.1.5 小练习与答案

**练习 1**：一个 45 帧、25fps 的短视频，默认参数下 `smart_nframes` 返回多少？

答案：\( n_{raw} = 45/25 \times 2 = 3.6 \)；`min_frames=4` 把它抬到 4；`max_frames = floor(min(768,45)/2)×2 = 44`，夹逼结果为 4；向下取偶仍为 4。**返回 4**。

**练习 2**：为什么最后对齐偶数用 `floor_by_factor` 而不是 `ceil_by_factor`？

答案：L177 已经把帧数夹到不超过 `max_frames` 和 `total_frames`，`ceil` 可能在取整时重新突破这些上限（例如夹逼后为 5、total_frames 为 5 时，`ceil` 到 6 就越界了），而 `floor` 保证对齐后必然仍在上限内。宁可少采一帧，不可超出预算。

**练习 3**：用户同时传了 `fps=2` 和 `nframes=16` 会怎样？

答案：L167 的 `assert` 直接失败，抛出 `AssertionError: Only accept either 'fps' or 'nframes'`。这与 transformers processor 中 `num_frames` 与 `fps` 互斥的约定（README 中 "set num_frames = 128 and overwrite the fps to None!"）是同一设计思想。

### 4.2 三种解码后端：torchvision、decord 与 torchcodec

#### 4.2.1 概念说明

「采样 N 帧」之前得先把视频解码成帧。三种后端的定位：

| 后端 | 包 | 特点 |
|---|---|---|
| torchvision | `torchvision`（必装依赖） | 调 `io.read_video`，**先整段解码再挑帧**，最慢但永远可用，是兜底 |
| decord | `decord`（可选，`pip install qwen-vl-utils[decord]`） | **按帧号随机访问**（`get_batch`），无需全量解码，社区常用的提速手段 |
| torchcodec | `torchcodec`（可选） | PyTorch 官方推出的解码库（FFmpeg 后端），支持 `get_frames_at` 按索引取帧 |

三者实现同一份契约：输入视频元素 dict，输出三元组 `(video 张量 [T,C,H,W], video_metadata 字典, sample_fps)`。

#### 4.2.2 核心流程

后端的选择发生在 `get_video_reader_backend`，优先级是：

```text
FORCE_QWENVL_VIDEO_READER 环境变量（强制指定，不校验是否安装）
    └─ 未设置 → 装了 torchcodec ？ → 用 torchcodec
                  └─ 没装 → 装了 decord ？ → 用 decord
                              └─ 没装 → 用 torchvision（兜底）
```

选定后，`fetch_video` 还有一层**运行时回退**：任何后端解码抛异常（包括强制指定了未安装的后端导致 `import decord` 失败），都会记录 warning 并改用 torchvision 重试。两层保障合起来：**强制指定只影响“先试谁”，试失败仍回到 torchvision**。

#### 4.2.3 源码精读

**后端一：torchvision（全量解码）**

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L205-L217](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L205-L217)

这段代码调用 `torchvision.io.read_video` 把视频**整段**读成 `(T,C,H,W)` 张量（L205-211，`video_start`/`video_end` 通过 `start_pts`/`end_pts` 交给 torchvision 处理）；L212 从返回值取出总帧数与原始 fps；L214 调用上一模块的 `smart_nframes`；L215 用 `torch.linspace(0, total_frames-1, nframes)` 生成**均匀分布的帧号**再四舍五入取整——这就是「均匀采样」的实现，一行搞定。

**起止帧换算：`calculate_video_frame_range`（decord / torchcodec 共用）**

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L260-L289](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L260-L289)

这个函数把「秒」换算成「帧号」：不传时间范围就返回 `(0, total_frames-1, total_frames)`（L262-263）；否则起点帧号 = `ceil(start × fps)`（L269，宁晚勿早），终点帧号 = `floor(end × fps)` 并夹到最后一帧（L275-276，宁早勿晚）；起止颠倒则抛 `ValueError`（L281-286）。**注意返回的第三个值会覆盖调用方的 `total_frames`**——它才是「有效时间范围内」的帧数，后续 `smart_nframes` 与 `sample_fps` 都基于这个新值计算。torchvision 后端不用它（时间裁剪已交给 `read_video` 的参数）。

**后端二：decord（随机访问）**

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L309-L319](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L309-L319)

decord 的 `VideoReader` 惰性打开视频：`len(vr)` 得总帧数、`vr.get_avg_fps()` 得平均 fps（L310），全程不解码全部帧；L317 在 `[start_frame, end_frame]` 上做 `linspace` 均匀采样；L318 `vr.get_batch(idx)` **只解码选中的那几帧**，再从 NHWC 转成 TCHW。这就是它通常比 torchvision 快的原因——解码量从「全部帧」降到「nframes 帧」。

**后端三：torchcodec**

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L353-L368](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L353-L368)

结构与 decord 几乎同构：`VideoDecoder` 的 `metadata.average_fps` / `metadata.num_frames` 提供视频信息（L358-359），`get_frames_at(indices=idx)` 按帧号取帧（L368）。差异点是 L353 的 `TORCHCODEC_NUM_THREADS` 环境变量（默认 8），用于控制底层 FFmpeg 线程数。

**三者的公共产物：metadata 与 sample_fps**

每个后端返回的 `video_metadata` 都含四个字段，以 torchvision 为例：

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L216-L225](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L216-L225)

- `sample_fps = nframes / total_frames × video_fps`：**实际**采样率。正常情况下等于你请求的 fps（如 300 帧采 20 帧、原始 30fps → 20/300×30 = 2.0）；但当理想帧数被 `min_frames` 抬高或被 `max_frames` 压低时，它会偏离请求值（如 15 帧的短视频采了 4 帧 → 4/15×30 = 8.0）。这就是为什么库要把真实采样率一路带回去交给 processor。
- `video_metadata`：记录原始 `fps`、实际抽中的 `frames_indices`、`total_num_frames` 和 `video_backend`。前三个字段是 Qwen3-VL 时间戳机制的原料，下一讲（u2-l4）展开。

**注册表与选择器**

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L380-L400](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L380-L400)

L380-384 把三个函数注册进字典；L386 在**模块导入时**读取 `FORCE_QWENVL_VIDEO_READER`；L389 的 `@lru_cache(maxsize=1)` 使选择结果**整个进程只算一次**；L399 把选中的后端名打印到 stderr（所以你会在控制台看到 `qwen-vl-utils using decord to read video.` 这行字）。注意 L391-392：强制指定的后端**不做安装校验**，直接返回字符串——真正的校验发生在调用失败后的回退里。

**回退逻辑（在 `fetch_video` 里）**

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L408-L414](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L408-L414)

L409 拿到后端名，L411 用它解码；L412-414 捕获**一切异常**（未安装、解码失败、文件损坏……），打 warning 后换 torchvision 再试一次。若 torchvision 也失败，异常会继续向上抛给调用者。

#### 4.2.4 代码实践

**实践目标**：亲手切换三种解码后端，对比同一视频的帧数、分辨率与耗时，并观察自动回退。

1. 操作步骤：

   先准备一个本地 mp4（可从 README 使用的示例视频下载：`https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen2-VL/space_woaudio.mp4`）。保存以下脚本（**示例代码**）：

```python
# 示例代码：try_backend.py
import logging, sys, time
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

from qwen_vl_utils import fetch_video

ele = {"type": "video", "video": sys.argv[1]}   # 例如 file:///home/you/demo.mp4

st = time.time()
video = fetch_video(ele, image_patch_size=16)    # Qwen3-VL 必须显式传 16
t, c, h, w = video.shape
print(f"耗时 {time.time()-st:.2f}s -> 帧数={t}, 单帧分辨率={w}x{h}")
```

   然后用 bash 循环分别以三种后端各启动**一个新进程**（切记不能在同一进程里切换，见 2.4）：

```bash
for b in torchvision decord torchcodec; do
  echo "===== backend: $b ====="
  FORCE_QWENVL_VIDEO_READER=$b python try_backend.py file:///path/to/demo.mp4
done
```

2. 需要观察的现象：
   - stderr 打印的 `qwen-vl-utils using xxx to read video.` 是否随环境变量变化；
   - `logging.basicConfig(level=logging.INFO)` 打出的后端日志（形如 `torchvision: video_path=..., total_frames=..., video_fps=..., time=...s`），可直接读到**解码耗时**；
   - 若某后端未安装（如没装 decord 却强制指定它），是否出现 warning 并回退 torchvision；
   - 三次运行的帧数与最终分辨率是否一致。
3. 预期结果（待本地验证）：三种后端的**帧数和分辨率完全一致**（`smart_nframes` 与 4.3 的预算逻辑与后端无关）；**耗时可能不同**，decord / torchcodec 按需解码通常快于 torchvision 全量解码，但具体数字以你机器实测为准。强制指定未安装的后端时会看到 `video_reader_backend decord error, use torchvision as default` 的 warning。
4. 把三组「帧数 / 分辨率 / 耗时」记入笔记，形成你的机器上三后端的性能对照表。

#### 4.2.5 小练习与答案

**练习 1**：机器上同时装了 decord 和 torchcodec、未设环境变量时，用哪个后端？

答案：torchcodec。优先级是 `FORCE_QWENVL_VIDEO_READER` > torchcodec > decord > torchvision（L391-398）。

**练习 2**：为什么在同一个 Python 进程里执行 `os.environ["FORCE_QWENVL_VIDEO_READER"] = "decord"` 后调用 `fetch_video` 不会切换后端？

答案：两个原因叠加——环境变量在模块 **import 时**已读取存入 `FORCE_QWENVL_VIDEO_READER`（L386），之后改 `os.environ` 不会更新它；且 `get_video_reader_backend` 带 `@lru_cache(maxsize=1)`（L389），首次调用的结果被缓存。因此切换后端必须重启进程。

**练习 3**：根 README 为什么推荐 `pip install qwen-vl-utils[decord]`（见 [README.md:L416-L420](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L416-L420)）？

答案：README 原文说明装上 decord 特性可获得更快的视频加载（"It's highly recommended to use `[decord]` feature for faster video loading"）。对应源码事实：装了 decord 后自动优先用它（L395-396），且它按帧号随机解码（`get_batch`）而不是全量解码，长视频场景提速明显。

### 4.3 fetch_video 主函数：total_pixels 如何决定单帧分辨率

#### 4.3.1 概念说明

解码采样解决了「时间维度」，`fetch_video` 的后半段解决「空间维度」：每帧该缩到多大？由三个参数（都写在视频元素 dict 里）联合决定：

- `min_pixels`：单帧**最小**像素数，默认 `128 token × 32² = 131072`（常量 `VIDEO_MIN_TOKEN_NUM=128`）；
- `max_pixels`：单帧**最大**像素数，默认 `768 token × 32² = 786432`（常量 `VIDEO_MAX_TOKEN_NUM=768`）；
- `total_pixels`：**整个视频**的总像素预算，默认 `MODEL_SEQ_LEN × 32² × 0.9 ≈ 1.18 亿像素`（约 11.5 万视觉 token 的 0.9 折）。

直觉：帧越多，每帧能分到的像素越少——否则一个 768 帧的长视频会撑爆上下文。`total_pixels` 正是「按帧数均摊」的总盘子。

#### 4.3.2 核心流程

`fetch_video` 中限制单帧上限的关键一行可以写成：

\[
max\_pixels_{eff} = \max\Big(\min\big(786432,\ \frac{total\_pixels}{nframes}\times 2\big),\ \ int(min\_pixels \times 1.05)\Big)
\]

三个成分的含义：

1. \( total\_pixels / nframes \)：总预算均摊到每帧；
2. **再 × 2（`FRAME_FACTOR`）**：因为时间 2× 压缩，两帧才产生一个时间步的 token，所以「每帧像素预算」可以放宽一倍而不增加 token 开销；
3. 外层 `max(·, min_pixels×1.05)`：保证算出的上限至少比下限高 5%，避免 `smart_resize` 的 `assert` 因上下限交叠而失败。

若用户显式给的 `max_pixels` 超过这个有效上限，库只打 warning 并**截断到上限**（不报错）。

得到 `min_pixels` / `max_pixels` 后，复用上一讲的 `smart_resize(height, width, factor=32, ...)` 计算单帧目标尺寸，再对整个视频张量做一次 BICUBIC resize。

视觉 token 总量估算式（自查用）：

\[
\text{video tokens} \approx \frac{nframes}{2} \times \frac{H}{32} \times \frac{W}{32}
\]

#### 4.3.3 源码精读

**预算常量的计算**

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L405-L407](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L405-L407)

`image_factor = image_patch_size × 2`（Qwen3-VL 为 32）；两行分别算出单帧像素的默认下限（128 token）与上限（768 token）。注意这里把「token 数」换算成「像素数」的方式就是乘 \( factor^2 \)。

**三层预算的联合**

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L448-L455](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L448-L455)

逐行解读：L448 从解码得到的张量取出帧数与单帧宽高（布局 [T,C,H,W]）；L449-450 读取 `min_pixels` 与 `total_pixels`（默认值由 `MODEL_SEQ_LEN` 派生，即 128000×1024×0.9 ≈ 1.18 亿像素）；L451 就是 4.3.2 的公式——注意 `total_pixels / nframes * FRAME_FACTOR` 里的 `FRAME_FACTOR=2` 正是时间压缩的补偿；L452-455 处理用户显式传 `max_pixels` 的情况：超上限打 warning，然后 `min` 截断。**结论：用户的 `max_pixels` 只能往下压，不能往上顶。**

**resize 执行**

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L456-L475](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L456-L475)

L456-461：与图像链路（u2-l2）一致的「直控尺寸」逃生口——同时给 `resized_height`/`resized_width` 时绕过 min/max 预算，只做 32 对齐（README 视频示例中 `resized_height: 280, resized_width: 280` 用的就是这条路，见 [qwen-vl-utils/README.md:L38-L39](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/README.md#L38-L39)）。L462-469：常规路径调 `smart_resize`（上一讲精读过的函数，约束仍是「32 整除 + 像素落区间 + 保长宽比」）。L470-475：对整个 [T,C,H,W] 张量一次 BICUBIC 插值（`antialias=True` 减轻缩小时的锯齿），并转 float。

官方文档对这组参数的建议见 [README.md:L609-L611](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L609-L611)：用 `min_pixels`/`max_pixels` 限制单帧分辨率，或用 `total_pixels` 限制总 token（建议低于 24576×32×32，防止输入序列过长）。

#### 4.3.4 代码实践

**实践目标**：验证「`total_pixels` 只压分辨率、不减帧数」，并手算验证 max_pixels 公式。

1. 操作步骤：运行以下脚本（**示例代码**），对同一视频设 4 档总预算：

```python
# 示例代码：budget_probe.py
from qwen_vl_utils import fetch_video

video_path = "file:///path/to/demo.mp4"
for total_tokens in [2048, 8192, 24576, 128000]:
    ele = {"type": "video", "video": video_path,
           "total_pixels": total_tokens * 32 * 32}
    video = fetch_video(ele, image_patch_size=16)
    t, c, h, w = video.shape
    per_frame = (h // 32) * (w // 32)
    print(f"总预算={total_tokens:>6} token -> 帧数={t}, 单帧={w}x{h}, "
          f"单帧 token={per_frame}, 视频总 token≈{t // 2 * per_frame}")
```

2. 需要观察的现象：帧数一列在 4 次运行中是否变化；单帧分辨率与单帧 token 是否随总预算单调变化；`视频总 token≈` 一列是否大致贴着预算上限。
3. 预期结果（待本地验证）：**帧数始终不变**（帧数只由 4.1 的 `smart_nframes` 决定）；预算越小单帧分辨率越低；总 token 不超过预算（`smart_resize` 的 floor 取整可能略低）。
4. 手算验证：设 demo 视频时长 10s、30fps，则 nframes=20；默认 `total_pixels` 下每帧上限 = min(786432, 117964800/20×2) = 786432 像素 = 768 token。若把总预算降到 20480×32×32（README 示例值），每帧上限变为 min(786432, 20480×1024/20×2) = 2097152 → 仍取 786432；继续降到 2048×32×32 时变为 min(786432, 209715) = 209715 ≈ 205 token。与你脚本打印的单帧 token 对照。

#### 4.3.5 小练习与答案

**练习 1**：nframes=512 的长视频、默认 `total_pixels` 下，每帧 max_pixels 上限是多少 pixel、多少 token？

答案：117964800/512×2 = 460800 pixel，低于 786432 的硬上限，故取 460800 ≈ 450 token/帧。（对比 nframes=20 时是 786432 = 768 token/帧——帧数越多每帧越小。）

**练习 2**：用户设 `max_pixels = 1000×32×32 = 1024000`，实际生效多少？

答案：1024000 > 硬上限 786432，L453-455 打 warning `The given max_pixels[...] exceeds limit[...]`，实际生效 786432。用户只能在 (min_pixels×1.05, 上限] 区间内往下压。

**练习 3**：想让长视频少占 token，改 `total_pixels` 和改 `fps` 各砍掉哪个维度的信息？

答案：`total_pixels`（或 `max_pixels`）压缩**空间**维——每帧分辨率下降、细节模糊；`fps`（或 `nframes`/`max_frames`）压缩**时间**维——帧变稀、动态细节丢失。总 token ≈ nframes/2 × 单帧 token，两条路都能降总量，选择取决于任务是「要看清画面里的小字」还是「要跟上车牌一闪而过的动作」。

### 4.4 帧列表输入路径：没有视频文件也能造视频

#### 4.4.1 概念说明

`ele["video"]` 不一定是字符串，也可以是**图片路径/PIL 对象的列表**。适用场景：视频文件还没拼接成 mp4（如监控截图、PPT 页面、逐帧导出的关键帧），或想精确控制「哪几帧」进入模型。README 明确支持这种写法（[README.md:L551-L572](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L551-L572)：帧列表 + `sample_fps`）。

这条路径**完全绕过解码器**——没有 mp4 就不需要解码，每张图直接走上一讲的 `fetch_image`。

#### 4.4.2 核心流程

```text
ele["video"] 是 list/tuple
  → 复制 ele，剔除 type/video 键 → process_info（保留 min_pixels 等预算键）
  → 线程池（≤8 线程）并行对每张图调 fetch_image（复用 u2-l2 图像链路）
  → 帧数向上补齐到偶数（不足则复制最后一帧）
  → 堆叠为 [T,C,H,W] 张量
  → 合成一份「假的」video_metadata（fps / frames_indices / total_num_frames）
  → 与文件路径汇合，进入 4.3 的预算与 resize 流程
```

合成的 metadata 里 `total_num_frames = (nframes / sample_fps) × raw_fps`，含义是：「这 nframes 帧按 sample_fps 的节奏排列，等价于 raw_fps 原始视频的第几帧」。这是为 Qwen3-VL 时间戳机制准备的原料（u2-l4 详述）。

#### 4.4.3 源码精读

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L415-L446](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L415-L446)

逐段解读这段代码：

- L417-420：断言输入是 list/tuple；`process_info` 复制元素 dict 并剔除 `type`/`video`，**剩下的键（min_pixels、max_pixels、resized_height 等）原样传给每一帧**——所以帧列表路径的预算参数语义与图像链路一致；
- L422-428：`ThreadPoolExecutor` 最多 `min(8, 帧数)` 个线程并行调 `fetch_image`（L425 把每张图包成 `{"image": ..., **process_info}`），图片解码是 IO 密集，并行收益明显；常量上限 8 来自 L35 的 `MAX_NUM_WORKERS_FETCH_VIDEO`；
- L430-432：`ceil_by_factor(len, 2)` 把帧数**向上**补到偶数——注意与 4.1 的 floor 方向相反：这里帧已经现成，多补一帧（复制最后一帧）比丢一帧划算；
- L434-438：`sample_fps` 默认 2.0；每张 PIL 图 HWC 转 CHW 后 `torch.stack` 成 [T,C,H,W]；
- L441-446：合成 metadata——`raw_fps` 可由用户指定（默认等于 sample_fps），`frames_indices` 就是 0..n-1（帧本来就是按序给的），`total_num_frames` 按上面的公式算出。

#### 4.4.4 代码实践

**实践目标**：用 4 张本地图构造「视频」，观察补帧行为与合成 metadata。

1. 操作步骤：准备 4 张图片，运行（**示例代码**）：

```python
# 示例代码：frames_list_demo.py
from qwen_vl_utils import fetch_video

ele = {
    "type": "video",
    "video": ["file:///path/to/frame1.jpg", "file:///path/to/frame2.jpg",
              "file:///path/to/frame3.jpg", "file:///path/to/frame4.jpg"],
    "sample_fps": 1.0,
}
video, metadata = fetch_video(ele, image_patch_size=16, return_video_metadata=True)
print("shape:", tuple(video.shape))
print("metadata:", metadata)
```

   再把列表改成 5 张图重跑一次。

2. 需要观察的现象：4 帧输入时 `shape[0]` 与 metadata 各字段；5 帧输入时 `shape[0]` 变成多少；宽高是否为 32 的倍数。
3. 预期结果（待本地验证）：4 帧输入 → shape 形如 `(4, 3, H, W)`，metadata 为 `{'fps': 1.0, 'frames_indices': [0,1,2,3], 'total_num_frames': 4.0}`（未传 `raw_fps` 时 fps 取 sample_fps，total = 4/1×1 = 4.0）；5 帧输入 → `shape[0] = 6`（ceil 到偶数、复制末帧补齐），metadata 的 total_num_frames 相应为 6.0。
4. 解释 `total_num_frames=4.0` 的含义：这 4 帧按每秒 1 帧排列，覆盖 4 秒；若原始视频是 24fps，可传 `"raw_fps": 24` 让它变成 4/1×24 = 96——即「等价于 24fps 原始视频的前 96 帧」。

#### 4.4.5 小练习与答案

**练习 1**：帧列表路径中，每张图的像素预算（min_pixels/max_pixels）从哪里来？

答案：来自视频元素 dict 的同名键——`process_info` 复制 ele 并剔除 `type`/`video` 后，把剩余键（含预算键）随 `{"image": ..., **process_info}` 传给每帧的 `fetch_image`（L418-428）；不传时用 `fetch_image` 的图像档默认值（4~16384 token）。

**练习 2**：为什么帧列表用 `ceil` 补齐偶数，而 `smart_nframes` 用 `floor` 对齐偶数？

答案：方向不同的原因在于「帧是否已经存在」。采样场景帧还没选，多采一帧就多一份解码与 token 开销，floor 保证不超预算；帧列表场景图片已在手上，复制末帧补齐几乎零成本，而丢掉一帧是真丢信息，所以 ceil。

**练习 3**：`{"video": [3 帧], "sample_fps": 2.0, "raw_fps": 24}` 的 total_num_frames 是多少？

答案：先补帧 3→4，再代入公式 (4/2)×24 = **48**。

## 5. 综合实践

把本讲四个模块串成一个「**视频预算体检脚本**」：对一个本地 mp4 输出完整诊断报告，并用 `fps=1` 与 `fps=4` 两次调用验证帧数符合 `smart_nframes` 公式（即本讲规格实践任务的后半部分）。

**任务**：编写 `video_checkup.py`（**示例代码**），对给定视频回答——用了哪个后端、原始多少帧/fps、按请求 fps 应采多少帧、实际采了多少、单帧被缩到多大、整个视频吃掉多少视觉 token。

```python
# 示例代码：video_checkup.py
import logging, os, sys
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

import decord  # noqa: F401  故意 import 一次：确保走 decord 后端（也可用环境变量强制）
from qwen_vl_utils import fetch_video
from qwen_vl_utils.vision_process import smart_nframes

video_path = sys.argv[1]

for fps in [1.0, 4.0]:
    ele = {"type": "video", "video": video_path, "fps": fps}

    # 第一步：只探测元信息（不解码像素），用于手算预测帧数
    vr = decord.VideoReader(video_path[len("file://"):] if video_path.startswith("file://") else video_path)
    total, vfps = len(vr), vr.get_avg_fps()
    predict = smart_nframes({"fps": fps}, total_frames=int(total), video_fps=vfps)

    # 第二步：完整走 fetch_video（解码 + 预算 + resize）
    video, meta = fetch_video(ele, image_patch_size=16, return_video_metadata=True)
    t, c, h, w = video.shape
    per_frame_tok = (h // 32) * (w // 32)

    print(f"\n[fps={fps}] 原始 {int(total)} 帧 @ {vfps:.2f}fps"
          f" -> 预测采 {predict} 帧 / 实际采 {t} 帧"
          f" | 单帧 {w}x{h} = {per_frame_tok} token"
          f" | 视频总 token ≈ {t // 2 * per_frame_tok}"
          f" | sample_fps={meta.get('frames_indices') is not None and t / max(int(total),1) * vfps:.2f}"
          f" | 后端字段={meta.get('video_backend', '帧列表合成')}")
```

**操作步骤**：

1. `pip install -e ./qwen-vl-utils` 与 `pip install decord`；
2. 准备一个本地 mp4（10 秒左右为宜），运行 `python video_checkup.py file:///path/to/demo.mp4`；
3. 对照输出，逐项核验：
   - 「预测采 N 帧」与「实际采 N 帧」两列是否相等（相等即证明 4.1 公式与真实解码路径一致；`smart_nframes` 在 `fetch_video` 内部就是用同样的 total_frames/video_fps 调用的）；
   - `fps=1` 与 `fps=4` 的帧数是否呈 1:4（以 10s/30fps/300 帧视频为例应为 10 与 40；若你的视频足够短，观察 `min_frames=4` 下限如何把两者都抬到 4）；
   - 视频总 token 是否随 fps 翻 4 倍而**超过** 4 倍（每帧上限 \( total\_pixels/nframes \times 2 \) 随帧数增多而下降，实际通常低于 4 倍——这正是 4.3 预算公式在起作用）。

**预期结果**（待本地验证）：帧数预测与实际一致；fps=4 时单帧分辨率低于 fps=1 时；两次的总 token 都不超过默认 `total_pixels` 预算。若任何一项不符，回到 4.1.2 与 4.3.2 的公式定位偏差来源。

## 6. 本讲小结

- **`smart_nframes`（L144-L181）**决定时间维度：\( nframes = \mathrm{floor}(\mathrm{clamp}(total\_frames/video\_fps \times fps,\ [4, max\_frames]) ,\ 2) \)，处处对齐偶数源于时间 2× 压缩；`fps` 与 `nframes` 互斥。
- **三种解码后端**实现同一契约 `(video[T,C,H,W], metadata, sample_fps)`；优先级 torchcodec > decord > torchvision，`FORCE_QWENVL_VIDEO_READER` 可强制但在 import 时读取且有 `lru_cache`（须换进程），任何失败自动回退 torchvision。
- **`sample_fps` 与 `video_metadata`** 记录的是「实际」采样结果（可能因 min/max_frames 偏离请求值），它们是下一讲时间戳机制的原料。
- **`fetch_video` 预算（L448-L455）**：单帧上限 \( \max(\min(768\,token,\ total\_pixels/nframes \times 2),\ min\_pixels \times 1.05) \)——`total_pixels` 只压空间分辨率，**不影响帧数**；用户 `max_pixels` 只能往下压。
- **帧列表路径（L415-L446）**绕过解码器，线程池并行复用 `fetch_image`，`ceil` 补偶数帧，并合成 `total_num_frames = (nframes/sample_fps) × raw_fps` 的元数据。
- 视觉 token 总量估算式：\( \text{tokens} \approx \frac{nframes}{2} \times \frac{H}{32} \times \frac{W}{32} \)。

## 7. 下一步学习建议

本讲拿到了 `fetch_video` 返回的 `video_metadata`（fps / frames_indices / total_num_frames / video_backend）却没展开它的用途。下一讲 **u2-l4「video metadata 与 Qwen3-VL 时间戳对齐机制」**正是讲它：Qwen3-VL 如何用这些字段把每一帧换算成文本可对齐的时间戳，实现「秒级事件定位」，以及 `return_video_metadata=True` 时 `process_vision_info` 返回值形态的变化。建议阅读顺序：

1. 先读本讲 4.4 的合成 metadata，再读 u2-l4；
2. 想了解 processor 侧如何消费这些张量（`video_metadata=`、`do_resize=False`、`do_sample_frames=False`），预习 [README.md:L639-L651](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L639-L651) 的完整调用示例；
3. 有余力的读者可对比根 README 中 processor 路径的视频预算（`processor.video_processor.size` 的 `longest_edge` 表示**全部帧总像素**，见 [README.md:L344-L356](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L344-L356)）与 qwen-vl-utils 路径的 `total_pixels`——两条路径的预算语义高度同构，理解其一即可举一反三。
