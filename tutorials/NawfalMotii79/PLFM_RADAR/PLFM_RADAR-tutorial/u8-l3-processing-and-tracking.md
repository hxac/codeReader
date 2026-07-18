# 信号处理、聚类与目标跟踪

## 1. 本讲目标

上一讲（u8-l2）我们已经把 USB 字节流拼成了一张完整的 Range-Doppler（距离-多普勒）图，并放进了 `RadarFrame`。但屏幕上用户最终想看到的不是一张热力图，而是**带编号、有航迹、落在地图上的目标点**。从「一堆被检测到的像素」到「稳定跟踪的目标列表」，中间还要走上位机的第二段处理。

本讲学完后，你应该能够：

- 画出 `RadarProcessor` 在上位机端的完整处理链：双 CPI 融合 → 检测提取 → DBSCAN 聚类 → 关联 → Kalman 跟踪 → 姿态修正与地理映射。
- 说清楚 DBSCAN 聚类为什么适合雷达点云、它的 `eps`/`min_samples` 各控制什么。
- 读懂 filterpy 的 4 状态 Kalman 滤波器如何对「距离 + 速度」做平滑预测，以及航迹是怎么被关联和淘汰的。
- 解释 IMU 俯仰角（pitch）如何修正目标仰角，以及 GPS 如何把极坐标 (距离, 方位) 换算成经纬度落到 Leaflet 地图上。
- 识别出代码中哪些是「已实现的真功能」、哪些是「接口已留好但当前是占位」的部分——这是阅读真实工程代码的关键能力。

## 2. 前置知识

在进入源码前，先用通俗语言铺几个概念。

**检测（detection）≠ 目标（target）**。FPGA 端的 CFAR（见 u4-l5）会逐个距离-多普勒单元判「有没有信号超过门限」，每个超门限的格子都是一个 detection。但一个真实目标（比如一架无人机）往往在相邻几个格子里都超门限，于是产生一簇 detection；反之噪声也可能偶尔孤立地超一次门限。所以上位机要把 detection **聚成簇**、再把帧与帧之间的簇**关联成航迹**，才算「目标」。

**CPI（Coherent Processing Interval，相参处理区间）**。雷达发射一组 chirp、收完再做 Doppler FFT 的这段时间叫一个 CPI，对应一张 Range-Doppler 图。把相邻两个 CPI 的结果融合，可以平滑噪声、提升检测稳定性——这就是「双 CPI 融合」。

**慢时间与 Doppler**。一张 Range-Doppler 图的两个轴：距离轴来自单个 chirp 内的快时间采样，速度轴来自多个 chirp 之间的慢时间相位变化（见 u4-l4）。所以每个检测点天然带 (range, velocity) 两个物理量。

**DBSCAN**。一种基于密度的聚类算法，不需要预先指定簇数，能把距离足够近、数量足够多的点归为一簇，并把孤立的噪声点标记为「噪声（label = −1）」。这正好匹配雷达点云「目标成簇、噪声离散」的特点。

**Kalman 滤波**。一种递推估计器：用「预测 + 修正」两步，把带噪声的测量值融合成一个对真实状态（位置、速度）的最优估计。在雷达里，它把每帧抖动的检测点平滑成一条连续航迹，并在偶尔丢一帧时靠预测「补位」。

**姿态修正**。雷达板装在载体（车、船、三脚架）上，载体如果前俯后仰（pitch），天线波束的实际指向就会偏离标称仰角，导致测出的目标仰角不准。用 IMU 读到的 pitch 角去补偿，就是姿态修正。

**两层 DSP 的分工**（这是理解本讲的关键背景）：FPGA 已经在硬件里跑了 DDC、匹配滤波、MTI、Doppler FFT、CFAR、DC notch（见 u4 系列）；上位机 `RadarProcessor` 里**也有一套**同名的 MTI/CFAR/加窗/DC notch，但它是「可选的主机端再处理」，用于在 PC 上重新算一遍或做实验，不是实时链路的必经之路。实时链路真正走的是「读 FPGA 给的 CFAR 检测标志 → 聚类 → 关联 → 跟踪 → 落图」。本讲的四个最小模块聚焦在后者。

## 3. 本讲源码地图

本讲涉及三个核心源码文件，全部在 `9_Firmware/9_3_GUI/v7/` 下：

| 文件 | 作用 |
|------|------|
| [processing.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/processing.py) | 信号处理与跟踪的核心算法库。定义 `RadarProcessor`（双 CPI 融合、MTI、CFAR、加窗、DBSCAN 聚类、关联、Kalman 跟踪）、`apply_pitch_correction`（俯仰修正）、`polar_to_geographic`（极坐标转经纬度）、`extract_targets_from_frame`（检测点→目标）。 |
| [models.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/models.py) | 数据类与配置。定义 `RadarTarget`（目标）、`GPSData`（含 pitch/heading）、`ProcessingConfig`（聚类/跟踪/DSP 开关与参数）、`WaveformConfig`（物理波形参数），以及 `SCIPY_AVAILABLE`/`SKLEARN_AVAILABLE`/`FILTERPY_AVAILABLE` 三个可选依赖标志。 |
| [map_widget.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/map_widget.py) | Leaflet 地图控件。把目标列表渲染成地图上的圆点、航迹、弹窗（弹窗里显示 range/velocity/azimuth/elevation/snr/track_id）。 |

此外还会引用装配这些算法的**调用方**：

| 文件 | 作用 |
|------|------|
| [workers.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/workers.py) | 后台工作线程。`RadarDataWorker._run_host_dsp` 是实时链路里把上述算法串起来的「总调度」，是本讲理解端到端流程的钥匙。 |

阅读建议：先看本讲第 4 节的四个模块理解算法本身，再回到 `workers.py:_run_host_dsp` 看它们怎么被串成一条流水线。

## 4. 核心概念与源码讲解

本讲的四个最小模块按实时数据流的方向组织：

1. **双 CPI 融合**——把多帧 Range-Doppler 平滑，提升检测质量（并说明「多 PRF 解模糊」的真伪）。
2. **DBSCAN 聚类**——把一簇簇检测点归并成候选目标。
3. **Kalman 跟踪（含关联）**——把帧间的候选目标关联成连续航迹并平滑。
4. **姿态修正与地理映射**——用 GPS/IMU 把目标落到地图坐标。

> 提醒：`RadarProcessor` 里还有 `mti_filter`/`cfar_1d`/`apply_window`/`dc_notch`/`process_frame` 等方法，它们是「主机端再处理」工具（见第 2 节背景），不在本讲四个核心模块里，但会在相关处顺带提及。

### 4.1 双 CPI 融合（与「多 PRF 解模糊」的真相）

#### 4.1.1 概念说明

单个 CPI 的 Range-Doppler 图里不可避免有随机起伏。如果把相邻两个 CPI 的距离像（range profile，即沿距离轴的功率分布）做平均后再叠加，噪声会因为不相关而被压低、真实目标的能量被保留，从而提升检测稳定性。这就是双 CPI 融合的直觉。

本模块还有一个**必须讲清楚的真伪问题**：`processing.py` 的模块文档字符串声称 `RadarProcessor` 提供「multi-PRF unwrap（多 PRF 解速度模糊）」，但**真实代码里这个方法并不存在**——它曾经存在、后来作为死代码被删除，并且有一条专门的回归测试锁死了它的删除（见 4.1.4）。 staggered-PRI（长短 chirp 交替）带来的解速度模糊能力，是**在 FPGA 硬件端**用双 16 点子帧 FFT 实现的（详见 u4-l4），上位机不再单独做一遍。阅读真实工程代码时，文档字符串滞后于代码是常态，**以代码与测试为准**是铁律。

#### 4.1.2 核心流程

双 CPI 融合是一个无状态的静态方法，输入两个距离像集合，输出一条融合距离像：

```text
输入: range_profiles_1  (形状 N1 × R，N1 个 CPI 各自的距离像)
      range_profiles_2  (形状 N2 × R)
步骤:
  1. 对集合 1 沿 CPI 维(axis=0)求均值  → mean1, 形状 R
  2. 对集合 2 沿 CPI 维(axis=0)求均值  → mean2, 形状 R
  3. 逐距离单元相加: fused[R] = mean1[R] + mean2[R]
输出: fused, 一条长度 R 的融合距离像
```

注意它是「均值相加」而非「均值再求均值」：两段 CPI 的均值各自代表了该段的稳态距离像，相加让目标能量叠加。数学上：

\[
\mathrm{fused}(r)=\frac{1}{N_1}\sum_{i=0}^{N_1-1}p_{1,i}(r)\;+\;\frac{1}{N_2}\sum_{j=0}^{N_2-1}p_{2,j}(r)
\]

其中 \(p_{k,i}(r)\) 是第 k 段第 i 个 CPI 在距离 r 处的功率。

#### 4.1.3 源码精读

模块文档字符串（注意其中「multi-PRF unwrap」的描述已与代码不符）：

[processing.py:L1-L12](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/processing.py#L1-L12) — 文件头说明，列出了 `RadarProcessor` 的职责（dual-CPI fusion、multi-PRF unwrap、DBSCAN、association、Kalman）。

`RadarProcessor` 类与双 CPI 融合的实现：

[processing.py:L278-L282](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/processing.py#L278-L282) — `dual_cpi_fusion` 静态方法，一行 `np.mean(..., axis=0)` 两次再相加，正是上面公式的直译。

```python
@staticmethod
def dual_cpi_fusion(range_profiles_1, range_profiles_2):
    """Dual-CPI fusion for better detection."""
    return np.mean(range_profiles_1, axis=0) + np.mean(range_profiles_2, axis=0)
```

> 实事求是地说：在 v7 的实时链路（`workers.py:_run_host_dsp`）里，并没有调用 `dual_cpi_fusion`——实时路径直接消费 FPGA 给的 CFAR 检测标志。它是作为算法库里的可用工具存在，供离线分析、回放（Replay）或未来扩展使用。老版本单文件 GUI（GUI_V5/V6）里曾直接使用过它。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `dual_cpi_fusion` 的「均值相加」语义，并确认 `multi_prf_unwrap` 确实已被删除。

**操作步骤**：

1. 在仓库根目录确保已 `uv sync --group dev`（见 u1-l4）。
2. 启动 Python，构造两段含一个目标峰、叠加随机噪声的距离像，调用融合函数：

```python
# 示例代码（非项目原有，用于理解算法行为）
import numpy as np
from v7.processing import RadarProcessor

rng = np.random.default_rng(0)
R = 64
# 两段 CPI，每段 8 帧，目标都在距离 bin=30，强度 10；噪声 ~ N(0,1)
p1 = rng.normal(0, 1, (8, R)); p1[:, 30] += 10
p2 = rng.normal(0, 1, (8, R)); p2[:, 30] += 10
fused = RadarProcessor.dual_cpi_fusion(p1, p2)
print("目标处融合值:", fused[30], " 噪声均值:", np.delete(fused, 30).mean())
```

3. 确认 `multi_prf_unwrap` 不存在：

```python
print(hasattr(RadarProcessor, "multi_prf_unwrap"))   # 预期 False
```

**需要观察的现象**：融合后目标 bin 处的值约为 \(10 + 10 = 20\)（两段均值各 10 相加），而噪声 bin 的值在 0 附近——目标被相对抬高。

**预期结果**：目标处 ≈ 20，背景噪声均值 ≈ 0；`hasattr(...)` 打印 `False`。

**回归测试佐证**：[test_v7.py:L257-L261](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/test_v7.py#L257-L261) 用 `assertFalse(hasattr(RadarProcessor, "multi_prf_unwrap"))` 锁死了删除，注释写明「multi_prf_unwrap was removed (never called, prf fields removed)」。这正是 4.1.1 里「以代码与测试为准」的硬证据。

> 如果本地未安装 numpy，上述脚本无法运行——属于「待本地验证」。但 `hasattr` 检查不依赖 numpy，可直接运行。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `dual_cpi_fusion` 改成「两段均值再取平均」`np.mean(np.mean(p1,0)+np.mean(p2,0))` 会怎样？它还返回距离像吗？

> **答**：不返回距离像了——它退化成一个标量（全距离的总体均值），丢失了「逐距离单元」的信息，无法用于检测。这解释了为什么代码用「相加」而不是「再求均值」：要保留沿距离轴的形状。

**练习 2**：模块文档字符串说有 multi-PRF unwrap，但代码里没有。你作为维护者该信谁？为什么？

> **答**：信代码和测试。文档字符串是给人看的说明，容易随重构滞后；回归测试 `test_no_multi_prf_unwrap` 是可执行的契约，明确该功能已删除。正确做法是顺手把文档字符串改成与代码一致（删掉「multi-PRF unwrap」字样）。

---

### 4.2 DBSCAN 聚类

#### 4.2.1 概念说明

CFAR 输出的检测点是「散点云」：一个真实目标常占据相邻的几个 (range, velocity) 单元，形成一个小簇；噪声则是零星孤立点。我们需要一种**不预设簇数**、能自动把密集点归簇、把孤立点当噪声丢掉的算法——DBSCAN（Density-Based Spatial Clustering of Applications with Noise）正合适。

DBSCAN 只有两个参数：

- `eps`：邻域半径。两个点若距离 ≤ eps，就算邻居。
- `min_samples`：成为「核心点」所需的最少邻居数（含自己）。

核心点连成一片就形成一个簇；邻居不够多的点被标为噪声（label = −1）。对雷达点云来说，「目标成簇、噪声离散」恰好让 DBSCAN 能把目标挑出来、把偶发虚警当噪声扔掉。

#### 4.2.2 核心流程

本项目的聚类在二维特征空间 `(range, velocity)` 上进行：

```text
输入: detections —— 一组 RadarTarget（每个含 .range 米、.velocity m/s）
参数: eps（邻域半径，默认 100）、min_samples（默认 2）
步骤:
  1. 把每个检测点映射成特征向量 [range, velocity]，堆成 X
  2. 调用 sklearn.cluster.DBSCAN(eps, min_samples).fit(X)
  3. 对每个簇标签 label != -1:
       - 收集该标签下所有点
       - 求簇中心 center = mean(点集)
       - 记录 {center, points, size}
输出: 簇列表（噪声点被丢弃）
```

为什么特征用 `(range, velocity)` 而不是 `(lat, lon)`？因为聚类要反映「物理上是不是同一个目标」，而同一目标在距离-速度空间里是聚拢的（它不会在一帧内既近又远、既快又慢）；经纬度还牵涉雷达自身位置，反而不稳定。

> 注意 `eps` 默认 100 的单位是「米」（range 维主导），因为 `RadarTarget.range` 单位是米、`.velocity` 单位是 m/s，二者量纲不同、数值尺度也差很多。这是一个已知的简化（见练习 2）。

#### 4.2.3 源码精读

可选依赖的优雅导入（聚类依赖 sklearn）：

[processing.py:L26-L27](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/processing.py#L26-L27) — `if SKLEARN_AVAILABLE: from sklearn.cluster import DBSCAN`。标志 `SKLEARN_AVAILABLE` 在 [models.py:L43-L48](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/models.py#L43-L48) 用 try/except ImportError 探测。没有 sklearn 时聚类直接返回空，不崩溃——这是 u8-l1 讲过的「优雅降级」。

聚类实现：

[processing.py:L286-L306](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/processing.py#L286-L306) — `clustering` 静态方法。关键三步：构造特征矩阵、fit、按标签分簇。

```python
points = np.array([[d.range, d.velocity] for d in detections])
labels = DBSCAN(eps=eps, min_samples=min_samples).fit(points).labels_

clusters = []
for label in set(labels):
    if label == -1:          # -1 是 DBSCAN 的噪声标记，跳过
        continue
    cluster_points = points[labels == label]
    clusters.append({
        "center": np.mean(cluster_points, axis=0),
        "points": cluster_points,
        "size": len(cluster_points),
    })
return clusters
```

调用方：[workers.py:L226-L228](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/workers.py#L226-L228) — 实时链路在提取出 `targets` 后，用配置里的 `clustering_eps`/`clustering_min_samples` 调用本方法得到 `clusters`，再交给关联（4.3 节）。

默认参数：[models.py:L169-L172](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/models.py#L169-L172) — `clustering_enabled=True`、`clustering_eps=100.0`、`clustering_min_samples=2`。`min_samples=2` 意味着只要两个点靠近就成簇，偏激进（宁可多聚类也不漏目标）。

#### 4.2.4 代码实践

**实践目标**：直观感受 `eps` 和 `min_samples` 对聚类结果的影响。

**操作步骤**：构造 8 个检测点——5 个挤在一起（模拟目标），3 个散开（模拟噪声），分别用不同参数聚类：

```python
# 示例代码（非项目原有）
from v7.processing import RadarProcessor
from v7.models import RadarTarget

def mk(r, v):
    return RadarTarget(id=0, range=r, velocity=v, azimuth=0, elevation=0)

dets = (
    [mk(500, 10) for _ in range(5)]        # 目标簇：同一(距离,速度)重复 5 次
    + [mk(2000, -50), mk(3000, 30), mk(4000, 0)]  # 3 个孤立噪声点
)

for eps, ms in [(100, 2), (100, 1), (3000, 1)]:
    c = RadarProcessor.clustering(dets, eps=eps, min_samples=ms)
    print(f"eps={eps}, min_samples={ms} -> 簇数={len(c)}, 各簇规模={[k['size'] for k in c]}")
```

**需要观察的现象**：
- `eps=100, ms=2`：5 个重合点成 1 个规模 5 的簇，3 个孤立点因两两距离 > 100 且不够 min_samples 被当噪声 → 簇数 1。
- `eps=100, ms=1`：`min_samples=1` 让每个点都成核心点，孤立点也各自成簇 → 簇数变多（噪声不再被丢弃）。
- `eps=3000, ms=1`：邻域半径过大，所有点可能被并成一个大簇。

**预期结果**：随 `eps` 增大，簇数先减后并；随 `min_samples` 减小，更多噪声点被保留为独立簇。这正是调参时「要滤噪就增大 min_samples、减小 eps」的依据。

> 若本地未安装 sklearn，`clustering` 会返回空列表 `[]`——这也是「优雅降级」的体现，属「待本地验证（依赖 sklearn）」。

#### 4.2.5 小练习与答案

**练习 1**：为什么噪声点的标签是 −1，而代码里要 `if label == -1: continue`？

> **答**：DBSCAN 用 −1 专门标记「未归入任何簇的噪声点」。代码 continue 跳过它们，使返回的 `clusters` 只含真实簇，不把噪声当目标送给后续跟踪。

**练习 2**：特征向量 `[range, velocity]` 把米和 m/s 混在一起算距离，这有什么问题？怎么改进？

> **答**：量纲不一致会让 range（动辄上千米）主导距离计算，velocity 几乎不起作用。改进做法是先对两维各自做标准化（z-score）或按物理意义加权，再送入 DBSCAN。当前实现是工程上的简化，理解它有助于你在调参时解释「为什么 eps 看起来是按米来生效的」。

---

### 4.3 关联与 Kalman 跟踪

#### 4.3.1 概念说明

聚类的输出是「本帧」的候选目标。但雷达要回答的是连续问题：「第 1 帧的目标 A，到第 2 帧还在不在？去哪了？」——这就需要两步：

- **关联（association）**：把本帧检测点与已有的航迹（track）一一配对。配上的检测点继承该航迹的编号；配不上的检测点开一条新航迹。
- **跟踪（tracking）**：用 Kalman 滤波器把配对上的「带噪声的测量」融合进航迹的状态估计，平滑位置、预测下一帧，并在偶尔丢检测时靠预测补位。

**Kalman 滤波的直觉**（不用公式先理解）：假设目标匀速运动。我有两个信息源——(a) 上一帧预测它「现在该到哪」，(b) 这一帧实测它「现在在哪」。两者都不完全准，于是我按各自的不确定度（协方差）做加权平均：越不确定的越不信。更新完后，再把状态往前推一步，得到下一帧的预测。如此递推，就得到一条平滑航迹。

本项目用 4 维状态、2 维观测的常速度（constant-velocity）模型：

- 状态 \(x=[r,\dot r,v,\dot v]^T\)：距离、距离变化率、速度、速度变化率。
- 观测 \(z=[r,v]^T\)：每帧只能直接测到距离和速度。

#### 4.3.2 核心流程

**关联**（最近邻 nearest-neighbour）：

```text
对每个检测点 det:
  遍历所有已有航迹 track, 算距离:
      dist = sqrt((det.range - track.state[0])^2 + (det.velocity - track.state[2])^2)
  取距离最小且 < 500 的航迹作为 best_track
  若找到: det.track_id = best_track       # 继承航迹编号
  否则:    det.track_id = 新编号(计数器自增)  # 开新航迹
```

**Kalman 跟踪**（对每个关联上的检测）：

```text
若该 track_id 是新航迹:
  初始化 KalmanFilter(dim_x=4, dim_z=2):
    x = [det.range, 0, det.velocity, 0]   # 初始状态，速率初值 0
    F = 常速度转移矩阵 (单位时间步 dt=1)
    H = 观测矩阵 (只观测 range 和 velocity)
    P *= 1000      # 初始不确定性很大
    R = diag(10, 1)  # 测量噪声：距离噪声 > 速度噪声
    Q = I*0.1      # 过程噪声
否则(已有航迹):
  kf.predict()                  # 预测：x = F x,  P = F P F^T + Q
  kf.update([det.range, det.velocity])  # 用新测量修正
  更新 track.state / last_update / hits
最后: 淘汰 > 5 秒未被更新的航迹
```

预测与修正的核心数学（filterpy 内部按此执行）：

预测步：
\[
\hat x^- = F\,\hat x,\qquad P^- = FPF^{\mathsf T}+Q
\]

更新步（\(K\) 为 Kalman 增益）：
\[
K = P^-H^{\mathsf T}\bigl(HP^-H^{\mathsf T}+R\bigr)^{-1}
\]
\[
\hat x = \hat x^- + K\bigl(z - H\hat x^-\bigr),\qquad P=(I-KH)P^-
\]

转移矩阵与观测矩阵（本项目实际取值）：
\[
F=\begin{bmatrix}1&1&0&0\\0&1&0&0\\0&0&1&1\\0&0&0&1\end{bmatrix},\qquad
H=\begin{bmatrix}1&0&0&0\\0&0&1&0\end{bmatrix}
\]

\(F\) 的两个 \(2\times2\) 对角块都是 \(\begin{bmatrix}1&1\\0&1\end{bmatrix}\)，即「位置 += 速度 × dt，速度不变」的常速度模型，且隐含 dt = 1（假设帧间隔均匀）。\(H\) 表示「只能直接测到 \(r\) 和 \(v\)」，把 4 维状态投影到 2 维观测。

#### 4.3.3 源码精读

可选依赖导入：[processing.py:L29-L30](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/processing.py#L29-L30) — `if FILTERPY_AVAILABLE: from filterpy.kalman import KalmanFilter`。标志定义在 [models.py:L50-L55](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/models.py#L50-L55)，缺 filterpy 时 `tracking` 直接 return，不报错。

航迹存储结构（在 `RadarProcessor.__init__` 里）：

[processing.py:L58-L67](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/processing.py#L58-L67) — `self.tracks: dict[int, dict]` 用 track_id 到航迹字典的映射；`self.track_id_counter` 是新航迹编号的发号器。

关联实现：

[processing.py:L310-L333](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/processing.py#L310-L333) — 最近邻关联。注意距离用 `track["state"][0]`（range）和 `track["state"][2]`（velocity），与 4 维状态的下标对应；门限硬编码为 500。

```python
for det in detections:
    best_track, min_dist = None, float("inf")
    for tid, track in self.tracks.items():
        dist = math.sqrt(
            (det.range - track["state"][0]) ** 2
            + (det.velocity - track["state"][2]) ** 2
        )
        if dist < min_dist and dist < 500:     # 500 是关联门限
            min_dist, best_track = dist, tid
    if best_track is not None:
        det.track_id = best_track              # 继承
    else:
        det.track_id = self.track_id_counter   # 新航迹
        self.track_id_counter += 1
```

Kalman 跟踪实现：

[processing.py:L337-L380](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/processing.py#L337-L380) — 新航迹初始化滤波器参数、已有航迹 predict/update、末尾淘汰过期航迹。滤波器参数直接对应 4.3.2 的公式：

```python
kf = KalmanFilter(dim_x=4, dim_z=2)
kf.x = np.array([det.range, 0, det.velocity, 0])
kf.F = np.array([[1,1,0,0],[0,1,0,0],[0,0,1,1],[0,0,0,1]])
kf.H = np.array([[1,0,0,0],[0,0,1,0]])
kf.P *= 1000            # 初始协方差大 → 早期更信测量
kf.R = np.diag([10, 1]) # 距离测量噪声 10，速度噪声 1
kf.Q = np.eye(4) * 0.1  # 过程噪声
...
track["filter"].predict()
track["filter"].update([det.range, det.velocity])
```

过期淘汰：[processing.py:L377-L380](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/processing.py#L377-L380) — `now - last_update > 5.0` 秒的航迹被删除，避免「目标早飞走了、航迹还赖着」。

实时链路的串联：[workers.py:L230-L232](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/workers.py#L230-L232) — 先 `association(targets, clusters)` 给每个检测点盖章 track_id，再 `tracking(targets)` 用 Kalman 更新对应航迹。两步必须按此顺序：先关联才知道谁归谁，再跟踪。

> 一个值得注意的细节：`association` 的距离门限 500 也是「米与 m/s 混算」（同 4.2 的量纲问题）。读真实代码时识别这种简化，比假装它很完美更有价值。

#### 4.3.4 代码实践

**实践目标**：用 filterpy 复现「预测-修正」一轮，观察 Kalman 增益如何在预测与测量之间折中。

**操作步骤**：

```python
# 示例代码（非项目原有，需 pip install filterpy numpy）
import numpy as np
from filterpy.kalman import KalmanFilter

kf = KalmanFilter(dim_x=4, dim_z=2)
kf.x = np.array([100.0, 0, 5.0, 0])   # 初始：距离 100m，速度 5m/s
kf.F = np.array([[1,1,0,0],[0,1,0,0],[0,0,1,1],[0,0,0,1]])
kf.H = np.array([[1,0,0,0],[0,0,1,0]])
kf.P *= 1000
kf.R = np.diag([10, 1])
kf.Q = np.eye(4) * 0.1

print("预测前 x =", kf.x)
kf.predict()                           # x = F x：距离 += 速率(0)，速度不变
print("predict 后 x =", kf.x)          # 仍 [100,0,5,0]（速率初值 0，距离没动）
kf.update([112.0, 5.0])                # 实测距离 112（比预测多 12），速度 5
print("update 后 x =", kf.x)           # 距离被拉向 112，但不会直接等于 112
```

**需要观察的现象**：`update` 后，`x[0]`（距离）会从 100 朝 112 方向移动，但**不会**一步跳到 112——因为初始 `P` 大、早期更信测量，但仍有 `R`（测量噪声）牵制；`x[1]`（距离速率）会被「修正出」一个非零值，说明滤波器从「测量位置在变快」推断出了速率。

**预期结果**：第一帧 update 后，距离估计介于 100 与 112 之间、速率被估出一个小正数。多跑几帧 `predict/update`，估计会逐步收敛到真实轨迹。这是「平滑」的来源。

> 若本地未装 filterpy，此脚本不能运行——属「待本地验证」。退而求其次，可以直接阅读 [processing.py:L337-L380](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/processing.py#L337-L380) 源码理解参数含义。

**源码阅读型实践**：即使没有 filterpy，回答下面这个问题——在 `tracking` 里，新航迹的 `kf.P *= 1000` 把初始协方差设得很大。结合 4.3.2 的更新公式 \(K=P^-H^{\mathsf T}(\cdot)^{-1}\)，思考：\(P\) 很大时 \(K\) 偏大还是偏小？这对「刚开局」的行为有什么好处？

> 参考答案：\(P\) 大 → \(K\) 大 → 更信测量、少信预测。好处是开局能快速对齐到真实位置，而不是被「速率初值 0」的错误预测拖住。随着更新推进 \(P\) 变小，滤波器逐渐变得「稳」。

#### 4.3.5 小练习与答案

**练习 1**：关联门限是 500，单位是什么？如果一个真实目标的航迹在两帧之间移动了 600（在 range-velocity 混合距离意义下），会发生什么？

> **答**：500 是 range（米）与 velocity（m/s）混合算出的「距离」门限。若目标移动量超过 500，检测点找不到任何已有航迹匹配，会被判为新航迹、`track_id_counter` 自增——于是同一条真实目标被切成两条航迹，出现「航迹断裂」。这是最近邻+硬门限的典型局限。

**练习 2**：状态向量是 4 维 `[r, ṙ, v, v̇]`，但观测只有 2 维 `[r, v]`。`ṙ` 和 `v̇` 这两个「速率」是怎么被估计出来的？

> **答**：它们不是直接测到的，而是 Kalman 滤波通过「多帧位置变化」隐式推断的。\(F\) 矩阵把速率耦合进位置（位置 += 速率），当连续几帧的 \(r\) 测量在变，更新公式就会把残差 \((z-H\hat x^-)\) 折算成对 `ṙ` 的修正。这正是「状态扩维、降维观测」滤波器的价值。

**练习 3**：为什么要在 `tracking` 末尾淘汰 5 秒未更新的航迹？

> **答**：目标可能已飞出探测范围、被遮挡或消失。若不淘汰，航迹字典会无限膨胀，且旧航迹可能被误关联上新出现的无关检测。5 秒是「目标 reasonable 丢失」的工程上限。

---

### 4.4 姿态修正与地理坐标映射

#### 4.4.1 概念说明

到这里，我们已经有了带 track_id 的目标，但它们还停留在「雷达极坐标」里：距离 r、速度 v、（标称）方位/仰角。用户想在地图上看到它们，还需要两步换算：

- **姿态修正**：雷达板若前俯后仰（pitch 角 φ），天线波束实际指向会比标称仰角偏低（或偏高），测出的目标仰角要减去 pitch 才是真实仰角。
- **极坐标 → 经纬度**：已知雷达自身的 GPS 位置和目标相对它的 (距离, 方位角)，用球面三角（haversine / bearing 公式）算出目标的经纬度，才能落到 Leaflet 地图上。

姿态数据（pitch、heading）和位置数据（lat/lon/alt）都封装在 `GPSData` 里，由 STM32 通过 USB CDC 把 GPS/IMU 报文送给上位机（见 u7-l1、u8-2 的 GPS 解析）。

> 一个真实代码里值得讲清的「占位」事实：当前实时链路里，FPGA **并不逐检测点上报仰角**，所以 `raw_elevation` 在 `workers.py` 里被硬编码为 `0.0`。于是 `apply_pitch_correction(0.0, pitch)` 的结果就是 `-pitch`。这说明接口和算法都已就位，只等 FPGA 端补上逐点仰角就能真正生效——读懂这种「半成品」状态，比误以为它已经在做精密仰角修正更重要。

#### 4.4.2 核心流程

**姿态修正**（一行公式）：

\[
\theta_{\text{corrected}}=\theta_{\text{raw}}-\varphi_{\text{pitch}}
\]

即「测得的仰角减去载体俯仰角」。pitch 为正（载体上仰）时，修正值变小，符合「波束被抬高了、要把读数往下压」的直觉。

**极坐标 → 经纬度**（球面 bearing 公式，R 为地球半径）：

\[
\varphi_2=\arcsin\bigl(\sin\varphi_1\cos\delta+\cos\varphi_1\sin\delta\cos\b\bigr)
\]
\[
\lambda_2=\lambda_1+\arctan2\bigl(\sin b\sin\delta\cos\varphi_1,\;\cos\delta-\sin\varphi_1\sin\varphi_2\bigr)
\]

其中 \(\delta = \text{range}/R_{\text{earth}}\) 是目标距离对应的角距离，\(b\) 是方位角（0 = 北，顺时针）。这正是 `polar_to_geographic` 实现的公式。

#### 4.4.3 源码精读

姿态修正函数：

[processing.py:L42-L48](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/processing.py#L42-L48) — `apply_pitch_correction(raw_elevation, pitch)` 直接 `return raw_elevation - pitch`。注释标明这是「Bug #4 fix — was never defined in V6」，即老版本漏定义、v7 补上的修正。

```python
def apply_pitch_correction(raw_elevation: float, pitch: float) -> float:
    """Apply platform pitch correction to a raw elevation angle.
    Returns the corrected elevation = raw_elevation - pitch."""
    return raw_elevation - pitch
```

极坐标转地理：[processing.py:L460-L484](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/processing.py#L460-L484) — `polar_to_geographic`，用 `R_earth=6_371_000` 米和上面的 bearing 公式把 (range, azimuth) 换成 (lat, lon)。

实时链路里的调用：[workers.py:L196-L210](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/workers.py#L196-L210) — 对每个检测点：`raw_elev = 0.0`（占位）→ 若有 GPS 则 `apply_pitch_correction(raw_elev, self._gps.pitch)` → 算 azimuth（单波束雷达无逐点方位，取航向 heading，见 `extract_targets_from_frame` 里的 ±15° 散开逻辑）→ `polar_to_geographic` 算经纬度。

```python
raw_elev = 0.0  # FPGA doesn't send elevation per-detection
corr_elev = raw_elev
if self._gps:
    corr_elev = apply_pitch_correction(raw_elev, self._gps.pitch)
...
if self._gps:
    azimuth = self._gps.heading
    lat, lon = polar_to_geographic(
        self._gps.latitude, self._gps.longitude, range_m, azimuth)
```

共享提取函数里的 ±15° 散开：[processing.py:L531-L540](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/processing.py#L531-L540) — 单波束雷达没有真实逐点方位角，于是把同一帧的多个检测点按 Doppler bin 在航向 ±15° 扇形里散开，让地图上的目标不至于全挤在一条线上。这是「显示层美化」而非物理测向。

GPS/IMU 数据结构：[models.py:L119-L130](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/models.py#L119-L130) — `GPSData` 含 `latitude/longitude/altitude/pitch/heading/timestamp`，其中 `pitch`（度）正是姿态修正的输入。

地图渲染：[map_widget.py:L431-L459](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/map_widget.py#L431-L459) — `updateTargetPopup` 把目标的 range/velocity/azimuth/**elevation**/snr/track_id 写进 Leaflet 弹窗；其中 elevation 显示的就是经过姿态修正（当前 = −pitch）的值。目标点本身按经纬度绘制（`updateTargets` 用 `t.latitude/t.longitude`），颜色按速度分级（`getTargetColor`）。

#### 4.4.4 代码实践

**实践目标**：验证 `apply_pitch_correction` 的代数语义，并理解「raw_elev=0 占位」对当前显示的实际影响。

**操作步骤**：

```python
# 示例代码（非项目原有，无外部依赖）
from v7.processing import apply_pitch_correction, polar_to_geographic

# 1) 姿态修正：载机上仰 3°
print("修正后仰角 =", apply_pitch_correction(10.0, 3.0))   # 预期 7.0
print("raw=0 占位时 =", apply_pitch_correction(0.0, 3.0))  # 预期 -3.0

# 2) 极坐标转经纬度：雷达在罗马，目标在正北 1000m
lat, lon = polar_to_geographic(41.9028, 12.4964, range_m=1000.0, azimuth_deg=0.0)
print("目标 lat, lon =", lat, lon)   # lat 应略增，lon 几乎不变
```

**需要观察的现象**：
- 仰角 10°、pitch 3° → 修正为 7°，符合「减去 pitch」。
- `raw_elev=0`（当前实时链路的真实情况）→ 结果 −3°，说明现在弹窗里显示的 elevation 其实就是 pitch 的负值，并不是真实测得的仰角。
- 正北 1000m 的目标，纬度比雷达略高、经度几乎不变（因为在正北方位）。

**预期结果**：`7.0`、`-3.0`、`(约 41.9118, 约 12.4964)`。

**源码阅读型实践（必做）**：打开 [workers.py:L196-L200](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/workers.py#L196-L200)，找到 `raw_elev = 0.0` 这行及其注释 `# FPGA doesn't send elevation per-detection`。然后回答：如果未来 FPGA 在 11 字节数据包里增加一个 elevation 字段（参见 u6-l1 的包格式），需要改动哪几处才能让姿态修正真正生效？

> 参考答案：(1) FPGA 端把逐点仰角塞进数据包；(2) `radar_protocol.py` 的解析把仰角读出来；(3) `workers.py` 里把 `raw_elev = 0.0` 改成从解析结果取值；(4) 可选：在 `RadarTarget` 已有的 `elevation` 字段里保存修正后的值。算法本身（`apply_pitch_correction`）不用改——这正是把接口先留好的价值。

#### 4.4.5 小练习与答案

**练习 1**：pitch 为正表示载体上仰。一个目标真实仰角是 5°，但载体上仰了 2°，雷达会测到多大的 raw 仰角？修正后应该回到多少？

> **答**：载体上仰 2° 让波束也抬高 2°，本应测到 5° 的目标会被读成约 7°（raw）。`apply_pitch_correction(7, 2) = 5`，修正回 5°。

**练习 2**：`polar_to_geographic` 用的是平面近似还是球面公式？为什么雷达近距离目标用哪种差别不大？

> **答**：用的是球面 bearing 公式（基于地球半径的 haversine 类推导）。雷达目标距离（几千米）相对地球半径（6371 km）极小，\(\delta=\text{range}/R\) 很小，平面近似与球面结果几乎一致；但用球面公式更严谨、在远距离也不引入系统误差。

**练习 3**：当前实时链路里 `raw_elev` 恒为 0，姿态修正结果恒为 −pitch。那这个函数现在是「真有用」还是「半成品」？

> **答**：是「算法与接口就绪、数据输入未接通」的半成品。它的价值在于：一旦 FPGA 端补上逐点仰角，无需改算法即可生效，且它已经修正了 V6 时代「根本没定义」的 Bug #4。识别半成品而非误读为成品，是读真实代码的重要能力。

---

## 5. 综合实践

把四个模块串起来，完成下面这个贯穿本讲的小任务。

**任务**：画出 `RadarProcessor` 从 Range-Doppler 图到「带航迹、落在地图上」的目标列表的完整处理步骤，并说明 `apply_pitch_correction` 如何用 IMU 俯仰角修正目标仰角。

**第一步：画处理链。** 请在你的笔记里画出下面的流程，并在每个箭头处标注「由谁、调了哪个方法、输入输出是什么」。参考答案如下（对照真实源码）：

```text
Range-Doppler 图(RadarFrame, 含 FPGA CFAR 检测标志 detections)
        │  workers.py:_run_host_dsp  (实时总调度)
        ▼
[逐检测点提取]  np.argwhere(frame.detections>0)
   ├─ bin→物理量: range_m = rbin*range_resolution
   │                velocity_ms = (dbin-16)*velocity_resolution
   ├─ 姿态修正(4.4): apply_pitch_correction(raw_elev=0, gps.pitch)
   └─ 地理映射(4.4): polar_to_geographic(lat,lon,range,azimuth)
        │  得到 list[RadarTarget]
        ▼
[DBSCAN 聚类(4.2)]  RadarProcessor.clustering(targets, eps, min_samples)
   └─ 在 (range,velocity) 空间把成簇点合并，丢噪声
        │  得到 clusters
        ▼
[关联(4.3)]  RadarProcessor.association(targets, clusters)
   └─ 最近邻(门限500)把检测点配到已有航迹，配不上则开新航迹
        │  每个检测点盖上 track_id
        ▼
[Kalman 跟踪(4.3)]  RadarProcessor.tracking(targets)
   ├─ 新航迹: 初始化 4 维常速度 KalmanFilter(P*=1000,R=diag(10,1),Q=0.1)
   ├─ 旧航迹: predict() + update([range,velocity]) 平滑
   └─ 淘汰 >5s 未更新航迹
        │  得到稳定航迹
        ▼
[地图渲染]  map_widget.set_targets(targets)
   └─ Leaflet 按 lat/lon 画圆点，弹窗显示 range/velocity/azimuth/elevation/snr/track_id
```

> 说明：图中的「双 CPI 融合（4.1）」当前不在实时链路里调用，它是离线/回放可用的算法工具；把它画在「Range-Doppler 图」之前作为可选的预处理（两个 CPI 距离像融合后再检测）。

**第二步：解释姿态修正。** 用你自己的话写出：IMU 测得载体俯仰角 pitch，`apply_pitch_correction(raw_elev, pitch) = raw_elev - pitch`，把「被载体姿态带偏的」标称仰角扣回去；当前因 FPGA 不逐点上报仰角，`raw_elev=0`，修正结果 = −pitch，弹窗里显示的 elevation 即此值。等 FPGA 补上逐点仰角后，此函数无需改动即可做真正的姿态补偿。

**第三步（可选，需依赖）**：运行 [test_v7.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/test_v7.py) 里与本讲相关的测试，验证你对行为的理解：

```bash
uv run pytest 9_Firmware/9_3_GUI/test_v7.py \
  -k "Clustering or PitchCorrection or ProcessFrame or PolarToGeographic or multi_prf" -v
```

观察 `test_clustering_empty`、`test_pitch_correction_*`、`test_no_multi_prf_unwrap` 等是否通过——它们正是本讲各模块行为的可执行契约。

## 6. 本讲小结

- 上位机的目标处理分为两段：`process_frame` 是可选的「主机端再 DSP」（DC notch/加窗/MTI/CFAR），实时链路真正走的是 `workers.py:_run_host_dsp` 的「提取 → 聚类 → 关联 → 跟踪 → 落图」。
- **双 CPI 融合**用两段距离像「均值相加」平滑噪声；模块文档里写的「多 PRF 解模糊」其实已被删除（有回归测试锁死），解速度模糊由 FPGA 双 16 点子帧 FFT 在硬件端完成（u4-l4）。
- **DBSCAN 聚类**在 (range, velocity) 空间把成簇检测点合并、把孤立噪声（label −1）丢弃，参数 `eps`/`min_samples` 控制聚类粒度；依赖 sklearn，缺失时优雅降级返回空。
- **关联**用最近邻（门限 500）把检测点配到已有航迹；**Kalman 跟踪**用 4 维常速度滤波器（状态 `[r,ṙ,v,v̇]`、观测 `[r,v]`）平滑航迹，并淘汰 5 秒未更新的航迹。
- **姿态修正** `apply_pitch_correction = raw_elev − pitch`；当前实时链路 `raw_elev=0`（FPGA 不逐点上报仰角），故结果为 −pitch，属「接口就绪、数据待接」的半成品。
- **地理映射**用球面 bearing 公式把 (range, azimuth) 换成经纬度，经 `map_widget` 落到 Leaflet 地图；单波束雷达用 ±15° 扇形散开做显示层美化。
- 阅读真实代码的元能力：文档字符串会滞后，**以代码与回归测试为准**；要能识别「真功能」「占位」「优雅降级」三种状态。

## 7. 下一步学习建议

- **横向对照 FPGA 端**：本讲的 MTI/CFAR/DC notch 在 FPGA 里都有一份硬件实现（u4-l3、u4-l4、u4-l5）。建议回看并对比：为什么这些 DSP 要放在 FPGA（实时、逐样本），而聚类/跟踪/姿态修正放在上位机（重浮点、非实时）？这呼应了 u2-l3 的三层分工判据。
- **GPS/IMU 数据来源**：`GPSData` 里的 pitch/heading 从哪来？追到 `USBPacketParser.parse_gps_data`（processing.py:399）和 STM32 端的 USBHandler（u7-l1、u8-2），理解一条 GPS 报文如何从 STM32 走到 `apply_pitch_correction`。
- **回放路径**：`extract_targets_from_frame`（processing.py:491）是 live 与 replay 共享的提取函数。建议阅读 `ReplayWorker`（workers.py），看离线回放如何复用本讲的算法链做数据分析。
- **测试体系**：本讲反复引用的 `test_v7.py` 属于 u11-l4（Python 测试与 lint）。学完那一讲，你将理解这些断言如何被 ruff 与 pytest 守护，以及 `test_no_multi_prf_unwrap` 这类「锁死删除」的测试在防止回归上的价值。
- **动手扩展**：若想把 4.4 的「半成品」补全，可按 4.4.4 的改动清单，端到端设计一个「FPGA 逐点仰角上报 → 上位机真实姿态修正」的功能扩展——这会同时牵动 FPGA 数据包格式（u6-l1）、Python 协议解析（u6-l2）与跨层契约测试（u11-l3），是一次很好的综合训练。
