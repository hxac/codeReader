# u5-l4 视频时序平滑与结果导出

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 Savitzky–Golay 滤波的数学原理，以及 `window_size` / `polyorder` 两个参数各自控制什么。
2. 走读 [app.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py) 中 `mesh_inference` 的「两遍结构」：第一遍逐帧收集 body/flame/cam 三组参数 → 在参数空间做时序平滑 → 第二遍重建网格并渲染。
3. 解释为什么平滑要放在**参数空间**而不是图像空间或顶点空间，以及三组参数使用不同窗口大小的动机。
4. 掌握 imageio + ffmpeg 写出浏览器可直接播放的 mp4 所需的三个关键编码参数（libx264 / yuv420p / faststart），以及偶数宽高约束的来源。
5. 通过窗口大小对比实验（3 / 7 / 21），建立「抖动抑制 vs 动作失真」的取舍直觉。

本讲是 u2-l4（Gradio 视频演示的工程外壳）的深入版：那一讲讲「流程怎么走」，本讲讲「平滑的数学、参数的组织方式、编码的细节」。

## 2. 前置知识

### 2.1 逐帧推理为什么会抖

PEAR 的推理是**逐帧独立**的：`ehm_model(img_patch)` 对每一帧单独前向，第 t 帧的输出完全不知道第 t-1 帧的结果。网络内部没有任何时序记忆（没有 LSTM、没有光流、没有上一帧状态输入）。于是即使视频中的人几乎静止，模型每帧预测的关节角度、相机尺度也会有微小随机波动——人眼对这种高频闪烁极其敏感，观感上就是「网格在发抖」。

通用解法是对预测序列做**时序低通滤波**：把信号里高频的随机分量压掉，保留低频的真实运动。

### 2.2 从滑动平均到 Savitzky–Golay

最简单的低通滤波是滑动平均（moving average）：把每个点替换成邻域窗口内点的算术平均。它的问题是「一刀切」——窗口多大，信号就被抹得多平，连真实的快速动作（挥手、眨眼）也会被削掉。

Savitzky–Golay 滤波（下文简称 SG 滤波或 savgol）的改进思路是：**在窗口内拟合一个多项式，而不是取平均**。因为多项式能精确表示「趋势 + 快变成分」，只要真实运动在窗口尺度内可以用低阶多项式近似，它就会被保留；只有多项式拟合不掉的高频噪声被滤除。SG 滤波因此是「保形滤波器」（shape-preserving），在化学光谱、心电信号处理中应用极广。scipy 提供了现成实现 `scipy.signal.savgol_filter`，PEAR 直接使用它（[app.py:108](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L108)）。

### 2.3 因果滤波 vs 非因果滤波

- **因果滤波**（如指数滑动平均）只使用当前和过去的数据，可以在线（流式）计算，但对称性缺失带来**相位延迟**——输出滞后于输入。
- **非因果滤波**（如 SG）使用以当前点为**中心**的对称窗口，需要「未来的帧」才能算当前帧的输出，只能离线处理整段序列，好处是**零相位延迟**：慢变信号不失真地通过。

这解释了 u2-l4 讲过的 `mesh_inference` 为什么必须拆成两遍：第一遍先把全部帧的参数收集完，才能开始平滑。这是数学性质决定的，不是工程偷懒。

### 2.4 视频编码三层概念

写出一个「浏览器 `<video>` 标签能播」的 mp4，需要同时满足三层约定：

| 层 | 概念 | PEAR 的选择 | 浏览器约束 |
|---|---|---|---|
| 容器 | 封装格式（.mp4） | mp4 | 几乎所有浏览器支持 |
| 编码器 | 帧间压缩算法 | H.264（libx264） | 各浏览器硬解支持最广 |
| 像素格式 | 像素存储布局 | yuv420p | Safari/Chrome 要求 4:2:0 子采样 |

另一个关键点是 **faststart**：mp4 文件里有一个记录索引的 `moov` box，默认写在文件末尾，浏览器必须下载完整个文件才能开始播放；faststart 把 `moov` 移到文件开头，实现边下边播。Gradio 前端用 `<video>` 标签播放结果，所以这些参数直接影响演示体验。

### 2.5 前置讲义

- **u2-l4**：app.py 的会话管理、`mesh_inference` 两遍结构的流程图、Gradio 事件模型（本讲不再重复）。
- **u3-l3 / u3-l4**：head 输出的 `body_param` / `flame_param` / `pd_cam` 各字段含义——本讲平滑的就是这些字段。
- **u4-l4**：EHM_v2 的 forward 四段流程——平滑之后的第二遍重建就是调用它。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [app.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py) | Gradio 视频演示入口 | `polynomial_smooth` 实现、三组参数的拆分—平滑—堆叠、mp4 编码写出 |
| [utils/get_video.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/get_video.py) | 图片序列合成视频的工具 | 与 app.py 相对的「简路径」，被 inference_images.py 使用 |
| [models/smplx/smplx_head.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py) | 解码头 | 确认被平滑的 pose 字段已经是旋转矩阵而非 6D 表示 |
| [models/modules/ehm/EHM_v2.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py) | 统一人体模型 | 消费平滑后参数的方式；`pose_params` 被置零的细节 |
| [models/modules/flame/lbs.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py) | LBS 蒙皮 | `pose2rot=False` 时直接把平滑后的矩阵元素拿来用、不做再正交化 |
| [requirements.txt](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/requirements.txt) | 依赖清单 | imageio / imageio-ffmpeg / scipy / decord 的版本固定 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**4.1 polynomial_smooth 实现**、**4.2 参数序列拆分与堆叠**、**4.3 视频编码写出**。三者正好对应 `mesh_inference` 的中间段与收尾段。

### 4.1 polynomial_smooth：Savitzky–Golay 滤波的工程封装

#### 4.1.1 概念说明

`polynomial_smooth` 是 app.py 里对 `scipy.signal.savgol_filter` 的一层薄封装，做的事情只有三件：把 GPU 张量搬到 numpy、做参数合法性检查、沿时间维滤波。它是 PEAR 消除逐帧抖动的唯一手段——仓库里没有滑窗平均、没有卡尔曼滤波、没有任何其他时序算法，全部时序一致性都押在这 14 行代码上。

理解它的关键是两个参数：

- **`window_size`（窗口宽度，必须为奇数）**：每次拟合用多少个相邻帧。窗口越大，滤波越强——噪声抑制越好，但快于窗口尺度的动作会被多项式模型「抹平」。直觉上窗口宽度 ≈ 你愿意用多长的时间邻域去解释当前帧。
- **`polyorder`（多项式阶数，必须小于窗口宽度）**：窗口内拟合的多项式阶数。阶数越高，对信号形状的保留越强、滤波越弱；当 `polyorder = window_size - 1` 时多项式自由度等于数据点数，拟合精确穿过每个点，滤波完全失效。PEAR 全部使用 `polyorder=2`（抛物线），即假设「窗口内的真实运动可以用二次函数近似」——匀加速运动模型。

#### 4.1.2 核心流程

SG 滤波对时间序列 \( y_1, \dots, y_N \)，取半宽 \( k \)（即 `window_size` \( = 2k+1 \)），对每个时刻 t 在窗口内做最小二乘多项式拟合，取**中心点**的拟合值作为输出：

\[
\hat{y}_t = p_t(0), \quad \text{其中 } p_t = \arg\min_{p \in \Pi_p} \sum_{j=-k}^{k} \left( y_{t+j} - p(j) \right)^2
\]

这里 \( \Pi_p \) 是次数不超过 p 的多项式集合。记设计矩阵 \( X \in \mathbb{R}^{(2k+1)\times(p+1)} \)，\( X_{j,m} = j^m \)，则输出可以写成**固定系数的卷积**：

\[
\hat{y}_t = \sum_{j=-k}^{k} c_j \, y_{t+j}, \quad c = e_0^{\top} (X^{\top} X)^{-1} X^{\top}
\]

从这个形式能直接读出三个重要性质：

1. **线性滤波**：输出是输入的加权和，对多维张量（如 (T, 21, 3, 3) 的姿态序列）就是逐元素独立地沿 axis=0 卷积。
2. **零相位**：系数关于 j 对称（\( c_j = c_{-j} \)），慢变信号无延迟通过——这是「离线两遍处理」换来的好处。
3. **退化关系**：当 \( p = 0 \) 时 \( c_j \equiv \frac{1}{2k+1} \)，SG 退化为滑动平均。`polyorder` 越高越「保形」。

边界处理用 `mode='interp'`：序列首尾各取最后一个窗口的点拟合多项式，直接外推边缘值。这带来一个硬约束——**序列长度必须不小于 `window_size`**，否则 scipy 抛异常。这正是 u2-l4 指出的「低帧率视频令平滑窗口越界崩溃」的根源：一段 3 秒、5 fps 的视频只有 15 帧，而代码默认窗口是 7（勉强够），一旦你按本讲实践改成 21 就会崩。

#### 4.1.3 源码精读

先看导入，savgol 来自 scipy：

[app.py:108](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L108) — 引入 `scipy.signal.savgol_filter`，scipy 版本固定在 [requirements.txt:61](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/requirements.txt#L61)（1.13.1）。

```python
def polynomial_smooth(sequence, window_size=5, polyorder=2):

    seq = np.asarray(sequence.cpu())
    if seq.ndim < 2:
        raise ValueError(f"输入必须至少是 2 维，当前 shape={seq.shape}")

    if window_size % 2 == 0:
        raise ValueError("window_size 必须是奇数")
    if polyorder >= window_size:
        raise ValueError("polyorder 必须小于 window_size")

    # Savitzky–Golay 沿着 axis=0 (时间维) 平滑
    smoothed = savgol_filter(seq, window_length=window_size, polyorder=polyorder, axis=0, mode='interp')
    return smoothed
```

完整实现见 [app.py:253-266](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L253-L266)，逐行解读：

- [app.py:255](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L255) — `sequence.cpu()` 先把 CUDA 张量搬回 CPU 再转 numpy。scipy 是纯 CPU 库，于是整个函数是一次「GPU → CPU → numpy → 滤波 → torch → GPU」的往返，调用方（如 [app.py:402](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L402)）拿到 numpy 数组后再 `torch.tensor(...).cuda()` 送回去。对每帧几百个浮点数的参数序列来说这个开销可以忽略。
- [app.py:256-257](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L256-L257) — 要求输入至少 2 维：滤波的对象是「(T, …) 的时间序列」，永远沿第 0 维（时间）走，所以一维标量序列必须先 unsqueeze。
- [app.py:259-262](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L259-L262) — 两条前置校验：窗口必须为奇数（对称窗口需要一个中心帧），多项式阶数必须小于窗口宽度（否则拟合自由度过剩、滤波失效）。注意这里没有校验「序列长度 ≥ window_size」，这个错误要等到 scipy 内部才爆出来——工程上的小缺口。
- [app.py:265](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L265) — 真正的滤波调用：`axis=0` 即时间维；`mode='interp'` 用首尾窗口的多项式拟合处理边缘帧。注意 savgol 对 shape 为 (T, 21, 3, 3) 的输入也是逐元素沿 T 卷积——这对下一讲的「旋转矩阵被逐元素平滑」至关重要。

#### 4.1.4 代码实践

本实践不需要 GPU、不需要模型权重，只需要 numpy / scipy / matplotlib（均已在 pear 环境中），目的是在纯合成信号上亲眼看到 `window_size` 与 `polyorder` 的效果。

**实践目标**：用一条「慢变正弦 + 快速脉冲 + 高斯噪声」的模拟信号复现抖动抑制与动作失真，量化两者随窗口增大的变化。

**操作步骤**：

```python
# 示例代码：save as smooth_lab.py，在仓库根目录 python smooth_lab.py 运行
import numpy as np
from scipy.signal import savgol_filter
import matplotlib.pyplot as plt

np.random.seed(0)
T = 300
t = np.arange(T) / 30.0                       # 10 秒 @30fps

slow = np.sin(2 * np.pi * 0.5 * t)            # 慢变"真实运动"：0.5 Hz
pulse = 1.0 * np.exp(-0.5 * ((t - 5.0) / 0.08) ** 2)   # 快速"动作"：约 5 帧宽的脉冲
noise = np.random.randn(T) * 0.05             # 逐帧"推理抖动"
y = (slow + pulse + noise)[:, None]           # 注意升到 2 维，否则 ndim<2 报错

print(f"{'window':>8} {'抖动(std of diff)':>18} {'脉冲峰值保留':>12}")
for w in (3, 7, 21, 41):
    ys = savgol_filter(y, window_length=w, polyorder=2, axis=0, mode='interp')
    jitter = np.std(np.diff(ys[:, 0]))
    peak_keep = ys[145:155, 0].max() / y[145:155, 0].max()   # 脉冲中心在 t=5s 附近
    print(f"{w:>8} {jitter:>18.4f} {peak_keep:>12.1%}")
    plt.plot(ys[:, 0], label=f"w={w}")

plt.plot(y[:, 0], 'k.', alpha=0.3, label="raw")
plt.legend(); plt.savefig("smooth_lab.png", dpi=120)
```

**需要观察的现象**：

1. 抖动指标（相邻帧差分的标准差）随窗口增大单调下降——噪声被压得越来越狠。
2. 脉冲峰值保留率随窗口增大骤降——w=3 时几乎完整保留，w=21 时快速动作被明显削平、变「软」。
3. 边缘帧（首尾几帧）与内部帧的平滑程度不同——`mode='interp'` 用单侧拟合处理边界。
4. 把 `polyorder` 从 2 改成 0 再跑一次：w=7/p=0 就是滑动平均，脉冲削得比 w=7/p=2 更狠——验证「多项式保形」的价值。
5. 把 `window_size` 改成 4：函数式校验（如果走 `polynomial_smooth`）或 scipy 内部会直接报错；把窗口改成 301（大于 T=300）：scipy 抛出 `window_length <= x.shape[axis]` 相关异常——亲手复现低帧率视频的崩溃条件。

**预期结果**：`smooth_lab.png` 中 w=3 的曲线紧贴原始信号（抖动残留明显），w=21 的曲线光滑但脉冲矮了一截，w=41 时慢变正弦本身也开始变形（0.5 Hz 信号在 41 帧 ≈ 1.37 秒的窗口里已经不再「低频」）。具体数值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `window_size` 必须是奇数？

**答案**：SG 滤波取的是「窗口中心点」的多项式拟合值，奇数宽度（2k+1）才能让窗口关于当前帧对称、存在唯一中心帧。偶数窗口没有中心帧，拟合值取在两个采样点之间，既破坏零相位性质也没有工程意义。代码在 [app.py:259-260](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L259-L260) 显式拒绝。

**练习 2**：`polyorder = window_size - 1` 时滤波输出是什么？为什么代码要禁止？

**答案**：此时多项式自由度等于窗口内数据点数，最小二乘拟合精确穿过所有点，\( \hat{y}_t \equiv y_t \)，滤波完全失效（输出等于输入）。代码在 [app.py:261-262](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L261-L262) 拒绝 `polyorder >= window_size`，把这种「看起来在滤波、实际上什么都没做」的静默错误挡在运行前。

**练习 3**：SG 滤波是零相位的，那网上的「大窗口导致动作延迟」说法为什么还会应验？

**答案**：严格说 SG 对「窗口内可用低阶多项式表示的信号」确实无延迟；但真实视频里的快速动作（挥手、眨眼）在 21 帧的窗口里不是二次曲线，而是窄脉冲。滤波会把脉冲削平、展宽、压低幅度——动作的**起始沿变缓**，观感上就是「迟钝、慢半拍」。所以更准确的说法是**快变动作的波形失真**，而不是数学意义上的相位延迟。本讲 4.1.4 实验里的「脉冲峰值保留率」就是量化这个失真的指标。

### 4.2 参数序列拆分与堆叠：两遍结构的中间层

#### 4.2.1 概念说明

u2-l4 已经画过 `mesh_inference` 的两遍结构。本模块精读中间那段 glue code：第一遍产出的三个 list（`body_sequence` / `flame_sequence` / `cam_sequence`，元素是每帧的参数字典或张量）如何被拆开、逐字段平滑、再重新组装成第二遍可用的逐帧字典。

先回答一个更根本的问题——**为什么在参数空间平滑，而不是图像空间或顶点空间？**

1. **图像空间不行**：对多帧渲染图做平均会得到「重影」——手臂在第 40 帧和第 41 帧位置不同，平均后出现两个半透明手臂，比抖动更难看。
2. **顶点空间可以但不划算**：每帧 10475×3 个浮点数，且顶点空间的平均虽然保持拓扑，却不能保证平均出来的姿态仍是 SMPL-X 参数化流形上的合法人体（顶点位置之间的关节耦合关系会被破坏的 risk 更高）。
3. **参数空间最合适**：每帧只有几百个数（身体 312 维姿态 + 缩放/形状/表情 + FLAME 约 425 维 + 相机 16 个矩阵元素），维度低、语义结构化（一个通道就是一个关节的自由度），且 PEAR 的参数直接驱动运动学链——姿态参数平滑了，关节运动自然平滑。低维滤波还天然避免了「平滑出非法人体」的问题，代价只是一个工程近似（见下）。

至于**三组参数分开平滑、窗口不同**，是代码里写死的事实：body 用 7、flame 用 5、cam 用 7。源码没有注释解释原因，但动机可以合理推断：脸部动作（眨眼约 100–300 ms、嘴唇开合）频率远高于躯干大动作且幅度小，给 flame 更小的窗口是避免把眨眼抹掉；cam 的尺度/平移抖动直接表现为整个网格全局晃动，给与 body 相同的 7。这一解读属合理推断，读者可自行验证（见综合实践）。

#### 4.2.2 核心流程

```text
第一遍（app.py:378-390）：逐帧 ehm_model 前向
    body_sequence  = [ {8 个非 None 字段}, ... × T ]     # 每帧一个 dict
    flame_sequence = [ {6 个字段}, ... × T ]
    cam_sequence   = [ (1,4,4), ... × T ]

拆分与堆叠（本模块）：
    for key in fields1/fields2:
        torch.cat([seq[key] for seq in ...], dim=0)      # 列表 → (T, ...) 张量
        polynomial_smooth(..., window_size=7 或 5)       # savgol 沿 axis=0
        torch.tensor(...).cuda()                          # numpy → GPU 张量
    cam_sequence = torch.cat(cam_sequence, dim=0) → 同样平滑

第二遍（app.py:439-470）：逐帧切片重建 dict
    body_dict[key]  = smoothed[key][idx:idx+1]            # (1, ...) 恢复 batch 维
    补回 3 个 None 键（eye_pose / jaw_pose / joints_offset）
    ehm(body_dict, flame_dict) → 顶点 → GS_Camera → 渲染
```

三个容易忽略的细节：

1. **被平滑的 pose 字段已经是旋转矩阵**。u3-l3 讲过 head 内部把 312 维 6D 表示经 `rot6d_to_rotmat` 转换后才装进 `body_param`（[smplx_head.py:286-289](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L286-L289)），所以 savgol 实际是对每个关节的 9 个矩阵元素**逐元素线性滤波**。9 个元素的凸组合一般不是正交矩阵（行列式 ≠ 1）——而下游 `lbs_wobeta` 在 `pose2rot=False` 时直接 `pose.view(batch_size, -1, 3, 3)` 使用、**不做再正交化**（[flame/lbs.py:312-317](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/flame/lbs.py#L312-L317)）。这是工程近似：帧间抖动小时相邻矩阵本就接近，其元素平均后的正交性偏差可忽略；若窗口开得过大导致快动作被「平均」，这个偏差会放大，蒙皮可能出现轻微体积变化。
2. **None 字段不能进滤波器**。`body_param` 共 11 键，其中 `eye_pose`、`jaw_pose`、`joints_offset` 恒为 None（[smplx_head.py:296-298](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L296-L298)），所以 `fields1` 只列了 8 个可平滑字段；第二遍重建 body_dict 时再把三个 None 手工补回去（[app.py:452-454](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L452-L454)），EHM_v2 内部对它们有自己的 None 分支处理。
3. **平滑发生在 EHM 之前**。EHM（FLAME 换头 + LBS 蒙皮）是非线性映射，参数平滑 ≠ 顶点平滑；但连续映射把「时间上平滑的参数」映成「时间上平滑的网格」，对消除高频抖动而言足够。另外一个有趣的边角：flame 字段里的 `pose_params` 平滑完会被 EHM_v2 强制置零（[EHM_v2.py:64](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L64)），这部分平滑工作是白做的（无害）。

#### 4.2.3 源码精读

**第一遍：收集**。[app.py:372-390](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L372-L390) 初始化三个空列表，循环内对每帧做 `pad_and_resize` → `to_tensor` → 前向（[app.py:381-385](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L381-L385)），然后只把三组参数 append 进序列（[app.py:388-390](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L388-L390)）。注意这一遍不做任何 EHM 重建和渲染——非因果滤波需要先看到整段序列。

**body 组拆分与平滑**：

```python
fields1 = [
    "global_pose", "body_pose", "left_hand_pose", "right_hand_pose",
    "hand_scale", "head_scale", "exp", "shape"
]
processed1 = {}
for key in fields1:
    data_list = [seq[key] for seq in body_sequence]
    data_tensor = torch.cat(data_list, dim=0)
    processed1[key] = torch.tensor(polynomial_smooth(data_tensor, window_size=7, polyorder=2)).cuda()
```

见 [app.py:393-402](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L393-L402)。这段代码做三件事：把 T 个 `(1, …)` 的帧级张量按字段抽出、`torch.cat(dim=0)` 堆成 `(T, …)` 时间序列、以 **window_size=7** 平滑后送回 GPU。8 个字段的平滑后形状分别是：global_pose (T,1,3,3)、body_pose (T,21,3,3)、left/right_hand_pose (T,15,3,3)、hand_scale (T,3)、head_scale (T,3)、exp (T,50)、shape (T,200)——形状来源见 head 的组装代码 [smplx_head.py:286-300](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L286-L300)。注意 `hand_scale`/`head_scale` 由同一个 6 维 scale 解码器切片而来（[smplx_head.py:292-294](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L292-L294)）。

**flame 组拆分与平滑**：同样的套路，见 [app.py:414-423](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L414-L423)，6 个字段（eye_pose_params / pose_params / jaw_params / eyelid_params / expression_params / shape_params），但 **window_size=5**。字段消费方 EHM_v2 期望的形状注释见 [EHM_v2.py:39-44](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L39-L44)。代码里那句「这里我猜你原意是从 eye_pose_params 取」的注释（[app.py:421](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L421)）是开发者自己的备注，实际逻辑是按 key 正确取值的。

**cam 组**：

```python
cam_sequence = torch.cat(cam_sequence, dim=0)
cam_sequence = torch.tensor(polynomial_smooth(cam_sequence, window_size=7, polyorder=2)).cuda()
```

见 [app.py:433-434](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L433-L434)。cam 序列堆成 (T,4,4) 后以 **window_size=7** 平滑。这里有个漂亮的不变量（见练习 3）：pd_cam 的旋转块恒为 diag(-1,-1,1)（u3-l4 的结论），常数序列经 SG 滤波后仍是同一常数——多项式可以精确表示常数，拟合残差为零——所以平滑**只实质作用于平移三分量和深度 z=24/s**，旋转块逐元素滤波后分毫不动。

**第二遍：逐帧切片重建**。见 [app.py:439-463](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L439-L463)：循环次数取 `global_pose.shape[0]`（即帧数 T），每帧用 `idx:idx+1` 切片把 (T,…) 的序列重新切成 (1,…) 的批形式，按 EHM_v2 期望的键名组装 `body_dict` / `flame_dict`，三个 None 键在 [app.py:452-454](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L452-L454) 补回。随后 [app.py:465-468](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L465-L468) 调 `ehm(body_dict, flame_dict)` 得网格、用平滑后的 `pd_cam` 构造 GS_Camera、渲染出 1024×1024 的 RGB 帧并转成 HWC numpy。网格从此只在第二遍出现——第一遍与第二遍之间隔着的，正是本模块的「拆分—平滑—堆叠」。

#### 4.2.4 代码实践

**实践目标**：不加载模型、不用 GPU，离线复演「拆分—堆叠—平滑—切片重建」这条 glue 链路，验证你对每个张量形状的记忆，并亲手触发 None 字段错误。

**操作步骤**：

```python
# 示例代码：save as seq_lab.py，在仓库根目录 python seq_lab.py 运行
# 注意：不要 import app —— app.py 模块级会初始化渲染器、下载权重（app.py:133-146）
import torch, numpy as np
from scipy.signal import savgol_filter

def polynomial_smooth(sequence, window_size=5, polyorder=2):   # 抄自 app.py:253-266
    seq = np.asarray(sequence.cpu())
    if seq.ndim < 2: raise ValueError("ndim < 2")
    if window_size % 2 == 0: raise ValueError("window must be odd")
    if polyorder >= window_size: raise ValueError("polyorder < window")
    return savgol_filter(seq, window_length=window_size, polyorder=polyorder, axis=0, mode='interp')

T = 30
shapes = {"global_pose": (1,1,3,3), "body_pose": (1,21,3,3),
          "left_hand_pose": (1,15,3,3), "right_hand_pose": (1,15,3,3),
          "hand_scale": (1,3), "head_scale": (1,3), "exp": (1,50), "shape": (1,200)}
body_sequence = [{k: torch.randn(*s) for k, s in shapes.items()} for _ in range(T)]

fields1 = list(shapes.keys())
processed1 = {}
for key in fields1:                                            # 复刻 app.py:399-402
    data_tensor = torch.cat([seq[key] for seq in body_sequence], dim=0)
    processed1[key] = torch.tensor(polynomial_smooth(data_tensor, window_size=7)).cuda()

for key in fields1:
    print(f"{key:>16}: {[s.shape for s in [torch.cat([seq[key] for seq in body_sequence], 0)]][0]} -> {tuple(processed1[key].shape)}")

# 逐帧切片重建（复刻 app.py:443-451），并补回三个 None 键
idx = 0
body_dict = {k: processed1[k][idx:idx+1] for k in fields1}
body_dict.update({"eye_pose": None, "jaw_pose": None, "joints_offset": None})

# 反面实验：把 None 键混进 fields1 会发生什么？
try:
    torch.cat([seq["eye_pose"] for seq in body_sequence], dim=0)
except Exception as e:
    print(f"\n混入 None 键后 torch.cat 报错: {type(e).__name__}")
```

**需要观察的现象**：8 个字段全部从每帧 `(1, …)` 堆成 `(30, …)` 再原样回来（savgol 不改形状）；切片 `idx:idx+1` 把形状还原为 `(1, …)`；混入 None 键后 `torch.cat` 直接抛 TypeError——解释了为什么 `fields1` 必须精确排除三个 None 字段。

**预期结果**：打印出的形状与本讲 4.2.3 列出的形状表一致；反例实验抛出 `TypeError`。GPU 缺失时把 `.cuda()` 删掉即可，不影响结论。

#### 4.2.5 小练习与答案

**练习 1**：把 flame 组的窗口也从 5 改成 21，眨眼（持续约 6 帧）最可能变成什么样？

**答案**：眨眼信号在 21 帧窗口里是无法被 2 阶多项式表示的窄脉冲，savgol 会把它削平、展宽——眼睑开合幅度明显减小，快眨可能完全消失（变成缓慢的眯眼）。这正是 flame 组用更小窗口 5 的理由：脸部动作频率高、幅度小，窗口必须短于动作本身的时间尺度。

**练习 2**：为什么不在顶点空间（对 10475 个顶点逐帧平滑）做？说出至少两条理由。

**答案**：其一，维度不划算——每帧 10475×3 ≈ 3.1 万个数 vs 参数空间几百个数，同样的窗口下计算量与内存放大近百倍；其二，参数空间有语义结构，一个通道对应一个关节自由度，平滑参数等价于平滑关节运动学，而顶点空间的线性平均不保证保持 SMPL-X 流形上的合法姿态耦合（关节联动、换头边界等约束都是参数空间的）；其三，参数平滑后仍走一遍 EHM 重建，非线性映射会重新生成拓扑完全一致的网格，而顶点平均只会让网格「变软」。

**练习 3**：`pd_cam` 的旋转块是常数 diag(-1,-1,1)，它也随 (T,4,4) 一起被逐元素平滑了，平滑后它会变吗？

**答案**：不会。savgol 是固定系数卷积，且最小二乘多项式可以**精确**表示常数序列（残差为零），拟合值就是常数本身——在边界处 `mode='interp'` 同样如此。所以旋转块滤波前后逐元素相等，平滑实际只影响平移列和深度项。可以在 4.2.4 的脚本里加一段 `cam = torch.eye(4).repeat(T,1,1); assert np.allclose(savgol_filter(cam, 7, 2, axis=0), cam.numpy())` 验证。

### 4.3 视频编码写出：浏览器兼容 mp4 的细节

#### 4.3.1 概念说明

第二遍渲染得到的 `all_meshes_img` 是一组 1024×1024 RGB numpy 帧，要变成 Gradio 前端 `<video>` 能直接播放的 mp4。这一步看似只是「存文件」，实则是演示体验的高频翻车点：H.265 编码、yuv444 像素格式、moov 后置的 mp4，都可能在部分浏览器（尤其 Safari）里黑屏或无法拖动进度条。

PEAR 在 app.py 里显式钉死了编码参数，并准备了失败回退；而仓库里还有第二条「简路径」——[utils/get_video.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/get_video.py) 的 `images_to_video`，被 inference_images.py 用来把 mesh_*.jpg 序列合成 video.mp4。两条路径的参数差异本身就是一份「哪些参数重要」的清单。

#### 4.3.2 核心流程

imageio 的 mp4 writer 底层调用 imageio-ffmpeg 自带的 ffmpeg 可执行文件（版本固定在 [requirements.txt:26-27](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/requirements.txt#L26-L27)），逐帧通过 stdin 喂给 ffmpeg 编码。影响输出的关键参数：

| 参数 | 取值 | 作用 | 不设置的后果 |
|---|---|---|---|
| `codec` | `"libx264"` | H.264 编码 | 各平台浏览器硬解支持最广的编码器 |
| `pixelformat` | `"yuv420p"` | 4:2:0 色度子采样 | 高像素格式在部分浏览器无法解码 |
| `ffmpeg_params` | `["-movflags","faststart"]` | moov box 前置 | 浏览器需下载完整文件才能起播 |
| `macro_block_size` | `None` | 禁用自动对齐 | 帧尺寸非 16 倍数时被静默缩放 |
| `fps` | `30`（硬编码） | 时间基准 | 与源视频 fps 不一致时播放变速 |

其中 yuv420p 有个硬约束：色度平面按 2×2 下采样，**宽高必须是偶数**——这是代码里那句 `img[: h - (h % 2), : w - (w % 2)]` 裁边存在的唯一原因。

#### 4.3.3 源码精读

**主路径：显式参数的 get_writer**。[app.py:485-492](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L485-L492)：

```python
writer = imageio.get_writer(
    mesh_video_path,
    fps=fps,
    codec="libx264",
    pixelformat="yuv420p",
    ffmpeg_params=["-movflags", "faststart"],
    macro_block_size=None,
)
```

五个参数各司其职：H.264 编码、4:2:0 像素格式、moov 前置、禁用宏块对齐缩放；`fps` 来自 [app.py:483](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L483) 的硬编码 `fps = 30`。注意一个不一致：截取输入视频时 fps 是从源视频元数据读的（[app.py:298](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L298)），而输出永远 30 fps——若源是 25 fps 的视频，重建网格视频会播放得比真实速度快 20%（u2-l4 已指出，根因在这里的两处 fps 来源不同）。

**偶数宽高裁剪**。[app.py:493-497](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L493-L497) 对每帧做 `img[: h - (h % 2), : w - (w % 2)]`——把奇数宽高裁成偶数。PEAR 的渲染帧是 1024×1024 天然满足约束，这行代码是防御性编程。仓库里另一处同样的操作是首帧预览的 `int(h * scale)//2*2`（[app.py:325](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L325)），动机相同。

**失败回退**。[app.py:499-501](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L499-L501)：显式参数的 writer 若抛异常（例如环境里 ffmpeg 不可用），退回 `imageio.mimwrite(mesh_video_path, all_meshes_img, fps=fps)`——不指定编码参数，一切交给 imageio 默认行为。这是「能出结果」优先于「结果完美」的降级策略。

**输入侧对比**：截取前 3 秒时用的 writer 只给了 `codec='libx264', quality=8`（[app.py:300](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L300)）——因为这个中间文件只给 decord 读（[app.py:361](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L361)），不需要浏览器兼容；输出文件要给浏览器，才把参数钉死。同一个 app 里「给谁读」决定「怎么编码」。

**RGB vs BGR**：渲染帧转 numpy 后保持 RGB 直接送 imageio（[app.py:468](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L468)，注意下一行被注释掉的 `COLOR_RGB2BGR`）——imageio/ffmpeg 期望 RGB；对比 inference_images.py 用 `cv2.imwrite` 落盘，必须先 `cv2.cvtColor(RGB2BGR)`（[inference_images.py:346](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L346)）。同一份渲染输出，写 jpg 与写 mp4 的颜色约定相反。

**简路径：utils/get_video.py**。[utils/get_video.py:16-47](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/get_video.py#L16-L47) 的 `images_to_video` 把目录下 jpg/png 按文件名末尾数字排序（[utils/get_video.py:6-14](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/get_video.py#L6-L14) 的 `sort_by_number`），全部读进内存后一次 `imageio.mimwrite(output_path, mesh_images, fps=fps)`（[utils/get_video.py:41-45](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/get_video.py#L41-L45)）写出——没有显式 codec / pixelformat / faststart，行为随 imageio-ffmpeg 默认值浮动。调用方 inference_images.py 以 `fps=30` 调它（[inference_images.py:370-374](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L370-L374)），函数签名默认值却是 25（[utils/get_video.py:16](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/get_video.py#L16)）——典型的「默认值与实际用法漂移」。

**附带产物 results.npz**。[app.py:503-507](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L503-L507) 保存顶点与面片：`faces` 来自渲染器拓扑（有效），但 `vertices_list` 的收集语句被注释（[app.py:472](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L472)），于是 npz 里的 vertices 恒为空数组——u1-l4 已确认这一点。这也顺带说明：当前 demo 的「参数级导出」链路没有打通，如何把它做出来正是 u5-l5 的二次开发主题。

#### 4.3.4 代码实践

**实践目标**：验证编码参数确实生效——用 imageio-ffmpeg 自带的 ffmpeg 二进制检查产出视频的流信息，并用字节级检查验证 faststart。

**操作步骤**：

```python
# 示例代码：save as codec_lab.py，在仓库根目录 python codec_lab.py 运行
import numpy as np, imageio, imageio_ffmpeg, subprocess

frames = [(np.full((1024, 1024, 3), v * 8 % 256)).astype(np.uint8) for v in range(30)]

# A：与 app.py:485-492 相同的显式参数
w = imageio.get_writer("a_full.mp4", fps=30, codec="libx264", pixelformat="yuv420p",
                       ffmpeg_params=["-movflags", "faststart"], macro_block_size=None)
for f in frames: w.append_data(f)
w.close()

# B：与 utils/get_video.py:41-45 相同的默认参数
imageio.mimwrite("b_bare.mp4", frames, fps=30)

exe = imageio_ffmpeg.get_ffmpeg_exe()          # 无需系统安装 ffmpeg
for path in ("a_full.mp4", "b_bare.mp4"):
    info = subprocess.run([exe, "-i", path], capture_output=True, text=True).stderr
    line = [l for l in info.splitlines() if "Video:" in l][0]
    print(path, "->", line.split("Video:")[1].strip()[:80])
    data = open(path, "rb").read()
    print(f"   moov@{data.find(b'moov'):>8}, mdat@{data.find(b'mdat'):>8},",
          "faststart 生效" if data.find(b'moov') < data.find(b'mdat') else "moov 在文件尾")

# C：奇数宽高实验——去掉 app.py:494-496 的裁边会发生什么
w = imageio.get_writer("c_odd.mp4", fps=30, codec="libx264", pixelformat="yuv420p", macro_block_size=None)
try:
    for f in [np.zeros((1023, 1023, 3), np.uint8)] * 4: w.append_data(f)
    print("C: 奇数帧未报错（ffmpeg 可能已自动处理，检查输出尺寸）")
except Exception as e:
    print(f"C: 奇数帧报错 {type(e).__name__}: {e}")
finally:
    w.close()
```

**需要观察的现象**：A 的流信息中包含 `h264` 与 `yuv420p`，且 `moov` 的字节位置在 `mdat` 之前（faststart 生效）；B 的流信息与 A 的差异（像素格式、`moov` 位置）取决于 imageio-ffmpeg 版本默认值——这正是 app.py 显式写参数的价值；C 中奇数宽高帧的行为（报错或自动处理）随版本而异，说明 [app.py:493-497](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L493-L497) 的裁边是把不确定性钉死的防御代码。

**预期结果**：A 显示 `h264 (High) ... yuv420p`；B 与 A 的差异项待本地验证（不同 imageio-ffmpeg 版本默认 pixelformat 可能相同或不同）；C 的具体报错信息待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：yuv420p 为什么强制宽高为偶数？

**答案**：4:2:0 子采样把彩色图像的色度通道在横竖两个方向各除以 2——每 2×2 个亮度像素共享一组色度值。宽或高为奇数时，色度平面出现半个像素，无法整除编码。代码用 `img[: h - (h % 2), : w - (w % 2)]` 把奇数裁成偶数（[app.py:494-496](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L494-L496)），首帧预览的 `//2*2`（[app.py:325](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L325)）同理。

**练习 2**：`-movflags faststart` 改变了文件的什么？为什么 Gradio 演示特别受益？

**答案**：mp4 的 `moov` box 存着帧索引（seek 表），ffmpeg 默认把它写在文件末尾（编码完才知道全部索引）。浏览器流式播放时是按字节范围下载的，`moov` 在尾部就必须等整个文件下载完才能解析出第一帧的位置。faststart 是编码完成后的二次封装：把 `moov` 搬到文件开头。Gradio 前端用 `<video>` 标签播放结果文件，moov 前置使点击后能立即起播、可随意拖进度条。

**练习 3**：`macro_block_size=None` 关掉的「自动宏块对齐」是什么？为什么 1024×1024 的渲染帧本来也不受影响？

**答案**：imageio 默认 `macro_block_size=16`，会把宽高不是 16 倍数的帧**静默缩放**到最近的 16 倍数——画面被意外拉伸或压扁且毫无提示。设 `None` 关掉这个行为，改由作者自己保证帧尺寸合法。1024 恰好是 16 的倍数（1024 = 64×16），所以 PEAR 的渲染帧无论开关都一样；但如果有人把渲染分辨率改成 1000，这个参数就是防止「悄悄变形」的保险。

## 5. 综合实践

把三个模块串起来做本讲的正式实践——**平滑窗口 3 / 7 / 21 的端到端对比实验**。

### 实践目标

在真实视频上量化「抖动抑制 vs 动作失真」的取舍，验证 4.1 合成信号实验的结论在完整管线中同样成立。

### 操作步骤

1. **修改三处窗口**。窗口值写在三处调用点（不是函数签名默认值）：body 组 [app.py:402](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L402)、flame 组 [app.py:423](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L423)、cam 组 [app.py:434](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L434)。按规格三组同步改：先全部 3，再全部 7（即当前 body/cam 的默认行为），最后全部 21。
2. **运行并抢救产物**。`python app.py` 启动后上传 `example/example_1.mp4`，点「Start Tracking Now!」。注意输出在 `temp_local/session_XXXXXX/results/mesh_video.mp4`，而会话目录在 600 秒后被延迟删除线程清掉（u2-l4 讲过的 `delete_later`）——**每次跑完立刻把 mp4 拷出来**并按窗口重命名（如 `mesh_w3.mp4`），否则前功尽弃。
3. **帧数自检**。开始前用 `decord.VideoReader(path)` 数一下截取后视频的帧数：example_1.mp4 若是 30 fps，3 秒约 90 帧，满足窗口 21 的下限（序列长度 ≥ window_size）；若帧数不足 21，第 3 轮会在 savgol 内部抛异常——这本身就是 4.1 讲的边界约束的现场验证。
4. **定性对比**。用 4.3.4 实验的 ffmpeg 命令或播放器逐帧对比三份视频，重点看两处：静止段落（网格是否仍在发抖）与快速动作段（挥手/转身是否变软、变慢）。
5. **定量对比（可选加分）**。在 [app.py:434](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L434) 平滑前后各加一行 `np.save(...)` 把 `cam_sequence.numpy()` 存盘，计算相邻帧差分的标准差作为抖动指标——三档窗口各跑一遍，得到与 4.1.4 合成实验同趋势的数字。
6. **写结论**。用三五行说清：哪档窗口抖动残留明显？哪档开始出现动作失真？对「以身体大动作为主」与「以面部表情为主」的两类视频，你各自推荐哪档？当前代码 7/5/7 的组合是否合理？

### 预期结果

- w=3：滤波强度低，静止段网格仍可见细碎抖动，快动作几乎无损。
- w=7（当前 body/cam 默认）：抖动基本消失，常规动作保真——这是作者选择的平衡点。
- w=21：极度平滑，但快速动作明显「发软」、眨眼可能消失；若视频帧数不足 21 直接崩溃。
- 定量指标上，抖动标准差应随窗口增大而下降，且下降幅度在 3→7 远大于 7→21（收益递减）。

具体观感与数值待本地验证。

## 6. 本讲小结

- PEAR 的时序一致性完全依赖一个 14 行的封装 `polynomial_smooth`（[app.py:253-266](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L253-L266)）：Savitzky–Golay 滤波 = 窗口内最小二乘多项式拟合取中心值，等价于固定系数的对称卷积，零相位、保形，但要求序列长度 ≥ window_size 且只能离线（两遍）处理。
- 平滑放在**参数空间**：逐字段把 T 帧堆成 (T,…) 序列，body 组 8 字段用窗口 7、flame 组 6 字段用窗口 5、cam 的 4×4 矩阵用窗口 7，再逐帧切片重建字典交给 EHM_v2；None 字段被排除在滤波之外、第二遍手工补回。
- 被平滑的 pose 字段在 head 内已转为旋转矩阵，savgol 是对其 9 个元素逐个线性滤波，结果只是**近似**正交矩阵，下游 `lbs_wobeta` 不做再正交化——窗口越大该近似越差。
- 浏览器兼容的 mp4 由五个显式参数保证：libx264 / yuv420p（要求偶数宽高，故有裁边代码）/ faststart（moov 前置可流式起播）/ macro_block_size=None（禁用静默缩放）/ fps 硬编码 30（与截取时读取源 fps 的做法不一致，非 30fps 源会变速）。
- 仓库有两条视频合成路径：app.py 的显式参数版与 [utils/get_video.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/get_video.py#L16-L47) 的默认参数简路径，后者默认 fps=25 与调用方传的 30 已经漂移。
- 平滑窗口的取舍是「抖动抑制 vs 快动作失真」的收益递减曲线：3→7 改善显著，7→21 改善有限但失真陡增；脸部参数用更小窗口是因为面部动作时间尺度更短。

## 7. 下一步学习建议

- **u5-l5 二次开发实践**：本讲 4.3.3 指出 demo 的参数级导出（results.npz 的 vertices 恒为空）没有打通，下一讲就动手写 `export_params.py`——逐帧保存 body_param / flame_param / pd_cam 为 npz 并实现离线重建渲染，把本讲的平滑与编码知识变成可复用的离线管线。
- **回看 u2-l4**：现在你理解了平滑的数学与编码细节，可以重新审视那一讲指出的三个工程硬伤（低帧率崩溃、fps 硬编码、延迟删除窗口），思考各自的修复补丁应该落在哪一行。
- **延伸阅读**：`scipy.signal.savgol_filter` 文档中关于 `mode` 各选项（mirror / nearest / interp）的边界行为；以及 ffmpeg 官方文档中 `-movflags faststart` 与 `-pix_fmt yuv420p` 的条目，对照本讲 4.3 的表格印证。
- 若对「零相位滤波 vs 因果滤波」感兴趣，可对比一阶 IIR / One-Euro Filter（VR 手部追踪常用）与 SG 在在线场景下的取舍——理解为什么 PEAR 的演示只能离线出片，而实时 AR 应用必须选 causal 滤波器。
