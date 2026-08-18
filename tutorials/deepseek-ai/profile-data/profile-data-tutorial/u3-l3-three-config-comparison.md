# 三种配置横向对比：train / prefill / decode

## 1. 本讲目标

前两单元我们把三份轨迹分别拆开看过：train.json 的 1F1B 注解、prefill.json 的双微批内核图谱、decode.json 的低延迟通信；u3-l1 沉淀了通用统计脚本，u3-l2 建立了区间求交的重叠率方法。本讲把这些碎片拼成一张完整的全景图。

学完本讲，你应该能够：

1. 独立整理三份轨迹在并行配置（EP64 / EP32 / EP128）与采集口径上的差异，并知道每项配置分别记录在 README、`distributedInfo` 还是 `traceName` 里。
2. 对比 normal（`dispatch`/`combine`/`notify`）与 low-latency（`dispatch_ll`/`combine_ll`）两代 DeepEP 通信内核在轨迹中的数量、时长占比与所在流形态。
3. 对比预填充 `compute_attn_ws` 与解码 `flash_fwd_splitkv_mla` 两代注意力内核的占比差异，并解释差异来自工作负载本身。
4. 用统一指标（通信占比、通信-计算重叠率、Top 内核）产出三份轨迹的横向对比结论，理解训练、预填充、解码在通信策略上的不同取舍。

本讲引用的统计数字，除特别说明外，均为本讲编写时用 `jq` 对仓库当前版本（HEAD `4496024`）三份 JSON 的实测值；文中给出的 Python 脚本可在本地复现这些数字（待本地验证）。

## 2. 前置知识

本讲是第三单元的综合课，默认你已完成 u1、u2 与 u3-l1/u3-l2。快速回顾将要复用的概念：

- **三个场景**：train（训练，一对前向/反向 chunk 的 DualPipe 调度）、prefill（推理预填充，长 prompt 一次性算完）、decode（推理解码，每步只生成一个 token）。术语详解见 u1-l1。
- **Chrome Trace 结构**：顶层四字段 `schemaVersion` / `deviceProperties` / `distributedInfo` / `traceEvents`（u1-l3）；事件八字段 `ph`/`cat`/`name`/`pid`/`tid`/`ts`/`dur`/`args`（u1-l4）；`kernel` 事件的 `args.stream` 就是流号（u2-l4）。
- **两代 DeepEP 内核**：normal 版 `dpsk::ep::internode::dispatch/combine/notify_dispatch/cached_notify` + `dpsk::ep::cuda::get_dispatch_layout`（u2-l3），与 low-latency 版 `dispatch_ll`/`combine_ll`（u2-l5）。dispatch 把 token 发往专家所在 GPU，combine 把结果聚合回来，合起来是 MoE 的 all-to-all 两个方向。
- **区间运算**：内核事件占用 GPU 的时间区间为左闭右开的 \([ts,\ ts+dur)\)（微秒）；把一个流上的区间排序合并得到"忙时"，两组区间的交集长度衡量并行度（u3-l2）。
- **CUDA Graph**：解码整条前向被录制为一张图、一次 `cudaGraphLaunch` 回放，轨迹中 3437/3647 个内核共享同一 correlation（7077），无法逐内核反查 CPU 算子（u2-l5）。

一个本讲新引入的提醒：**横向对比的前提是口径统一**。三份轨迹的采集窗口长度、批量定义、注解习惯都不同，直接比绝对时长没有意义；我们要比的是占比、形态与机制。

## 3. 本讲源码地图

| 文件 | 角色 | 规模与形态 |
|---|---|---|
| [README.md](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md) | 三份轨迹唯一的人读说明：并行配置、重叠策略、"模拟绝对均衡 MoE 路由"前提 | 31 行 |
| [train.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json) | 训练轨迹：EP64、DualPipe 一对 chunk、含 42 条调度注解 | 97653 行，格式化多行，约 3.1 MB |
| [prefill.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json) | 预填充轨迹：EP32、双微批、事件数最多 | 570649 行，格式化多行，约 17.5 MB |
| [decode.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json) | 解码轨迹：EP128、low-latency 通信、CUDA Graph 回放 | **单行压缩**（无换行符），约 4.7 MB，只能整体解析 |

注意 decode.json 是单行文件，行号锚点只有 L1——本讲引用它时统一标注为"单行文件，行号不适用"，引用 train.json / prefill.json 时给出精确行区间。

## 4. 核心概念与源码讲解

### 4.1 EP64 / EP32 / EP128：配置与采集口径对比

#### 4.1.1 概念说明

三份轨迹是同一个模型体系（DeepSeek-V3/R1，隐藏维 7168）在三种工作负载下的切片。配置信息散落在三个地方，可信度依次递减：

1. **README 正文**：人写的场景描述，最权威但最粗（只到 EP/TP/序列长度级别）。
2. **`distributedInfo`**：PyTorch Profiler 自动记录，`world_size` 直接可查（EP 并行时 world_size 即 EP 数，TP=1）。
3. **`traceName`**：导出时的原始文件名，常编码了实验细节（批量、SM 数等），属于"事实线索"而非文档。

对比时要特别留意三条**口径差异**，它们会误导粗心的对比者：

- **采集窗口不同**：train 只录一对 forward/backward chunk（1F1B 注解约 112ms），prefill 录的是完整预填充（GPU 内核总时长约 3.75s），decode 录约 84ms 的一段解码步。绝对时长不可比，只有占比可比。
- **注解习惯不同**：train 有 42 条框架打点的调度注解（`1F1B`、`attn(F)` 等），prefill 只有 6 条、decode 只有 3 条——且后两者的注解全部是 `nccl:all_reduce`（NCCL 库自动打点），没有任何调度注解。推理轨迹的分析只能靠内核指纹，这正是 u2-l4/u2-l5 的做法。
- **模拟前提**：三份数据都采集于"模拟绝对均衡的 MoE 路由"之下（README 第 3 行），通信量是理想值。

#### 4.1.2 核心流程

整理配置对比表的过程：

1. 从 README 三个小节分别摘出场景配置；
2. 解析三份 JSON 的 `distributedInfo`（world_size）与 `deviceProperties`（卡数、型号）；
3. 读 `traceName` 补充批量细节；
4. 统计 `traceEvents` 长度与 `cat=="user_annotation"` 数量，标注注解口径。

#### 4.1.3 源码精读

README 对训练配置的说明——EP64、TP1、4K 序列，且 PP 通信未计入轨迹：

- [README.md:L11-L12](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L11-L12)：说明 train.json 展示的是 DualPipe 中一对前向/反向 chunk 的重叠，每个 chunk 含 4 个 MoE 层；并行配置对齐 DeepSeek-V3 预训练（EP64、TP1、4K 序列长度）；为简化，profiling 不含 PP 通信。

README 对预填充与解码配置的说明：

- [README.md:L22](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L22)：prefill 用 EP32、TP1（对齐 V3/R1 线上部署），prompt 4K、每 GPU 16K tokens 批量；两个微批重叠计算与 all-to-all，且两微批注意力负载均衡（同一 prompt 可能被拆到两个微批）。
- [README.md:L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30)：decode 用 EP128、TP1、4K prompt（贴近线上配置），每 GPU 128 requests；同样双微批，但 all-to-all 通信不占 SM——RDMA 消息发出后全部 SM 释放，计算结束后再等待通信完成。

轨迹文件内的机器可读配置：

- [train.json:L70](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L70)：`distributedInfo` 为 `{"backend": "nccl", "rank": 0, "world_size": 64}`——EP64 的直接证据；`rank: 0` 提醒这是单进程视角。
- [train.json:L4-L12](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L4-L12)：deviceProperties 第一块卡，NVIDIA H800、132 SM、约 79.1 GiB 显存、compute capability 9.0；train 记录了 8 块卡（数组延续到 L69），prefill/decode 只记 1 块。
- [prefill.json:L14](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L14)：`world_size: 32`——EP32 的直接证据。
- decode.json（单行文件，行号不适用，见 [decode.json:L1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1)）：`distributedInfo` 为 `{"backend": "nccl", "rank": 0, "world_size": 128}`——EP128 的直接证据。

`traceName` 是三份文件里最"有味道"的配置指纹：

- [train.json:L97653](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97653)：`traces/prof_ep64_tp1_varsm_hidden7168_seqlen4096.json`——编码了 EP64、TP1、隐藏维 7168、序列 4096，`varsm` 暗示可变 SM 的均衡路由模拟。
- [prefill.json:L570649](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L570649)：`./dsv3-600B-tp1-ep32-input4096-output1-bs16384-split1-sm24.json`——编码了模型（dsv3-600B）、input 4096 / output 1（预填充特征）、bs16384（每 GPU 16K tokens）、split1、sm24。
- decode.json 的 `traceName` 为 `./decode-debug-rank0.json`（单行文件，行号不适用）——没有编码任何配置，解码的配置只能依赖 README 与 `distributedInfo`。

汇总为配置对比表（实测）：

| 配置项 | train.json | prefill.json | decode.json |
|---|---|---|---|
| 场景 | 训练（一对 F/B chunk） | 预填充 | 解码 |
| EP（= `world_size`） | 64 | 32 | 128 |
| TP | 1 | 1 | 1 |
| 序列/批量 | 4K 序列 | 4K prompt，16K tokens/GPU | 4K prompt，128 requests/GPU |
| `deviceProperties` 卡数 | 8 | 1 | 1 |
| `traceEvents` 总数 | 14240 | 85422 | 19417 |
| `user_annotation` 数 | 42（DualPipe 调度注解） | 6（全为 `nccl:all_reduce`） | 3（全为 `nccl:all_reduce`） |
| 重叠机制（README 自述） | DualPipe 前反向分块 | 双微批 | 双微批 + 通信不占 SM |

一个容易被忽略的细节：三份文件的 `process_labels` 元数据都画了 CPU + GPU 0~7 共 8 条 GPU 轨道（u1-l2 讲过这是查看器版式），但**有内核的 GPU 进程都只有 GPU 0 一个**——8 块卡的 `deviceProperties` 只出现在 train 里，更多是导出时环境信息的差异，不代表 train 真用了 8 卡。

#### 4.1.4 代码实践

**实践目标**：不依赖 README，仅从 JSON 本体提取三份轨迹的配置指纹。

**操作步骤**（示例代码，可直接运行）：

```python
# compare_config.py —— 提取三份轨迹的配置层信息（示例代码）
import json

for f in ["train.json", "prefill.json", "decode.json"]:
    d = json.load(open(f))
    ev = d["traceEvents"]
    ann = [e for e in ev if e.get("cat") == "user_annotation"]
    ann_names = sorted({e["name"] for e in ann})
    print(f, {
        "world_size": d["distributedInfo"]["world_size"],
        "gpus_in_props": len(d["deviceProperties"]),
        "gpu_model": d["deviceProperties"][0]["name"],
        "traceName": d.get("traceName"),
        "traceEvents": len(ev),
        "user_annotation": len(ann),
        "annotation_names": ann_names[:5],
    })
```

也可以用一条 `jq` 快速抽查（本讲编写时实际使用的方式）：

```bash
jq '{world: .distributedInfo.world_size, gpus: (.deviceProperties|length),
     events: (.traceEvents|length), name: .traceName}' decode.json
```

**需要观察的现象**：三份文件的 `world_size` 分别是 64/32/128；train 的 `traceName` 里能肉眼认出 `ep64` 字样，prefill 的能认出 `ep32` 与 `bs16384`；train 的注解名含 `1F1B`，另两份只有 `nccl:all_reduce`。

**预期结果**：与 4.1.3 的配置对比表逐项一致。若你的统计与表中数字不符，优先检查是否把 `ph != "X"` 的非完成事件也计入了。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 `distributedInfo` 里的 `world_size` 在这套轨迹里就等于 EP 数？

**答案**：README 明示三份配置均为 TP1（[README.md:L12](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L12)、[L22](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L22)、[L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30)），且采集说明只提专家并行；TP=1 时一个进程对应一块卡，`world_size` 即进程数，MoE 专家分布在这些进程上，故 world_size = EP。

**练习 2**：三份文件里哪一份的配置信息最依赖 README？为什么？

**答案**：decode.json。它的 `traceName` 只是 `./decode-debug-rank0.json`，不含任何配置编码；JSON 本体只能给出 world_size 128 与 H800 卡型，"128 requests/GPU、4K prompt、通信不占 SM"这些关键信息全部来自 [README.md:L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30)。

**练习 3**：train 记录了 8 块 H800 的 `deviceProperties`，能说明训练只用 8 卡吗？

**答案**：不能。`distributedInfo.world_size` 是 64（[train.json:L70](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L70)），`deviceProperties` 只是本进程可见的设备枚举，且轨迹是 rank 0 的单进程视角（u1-l2/u1-l3 都强调过：963 个内核全部落在 GPU 0）。

### 4.2 通信内核族对比：normal 两代成员 vs low-latency 极简两件套

#### 4.2.1 概念说明

MoE 的 all-to-all 通信在 DeepEP 里有两代实现（u2-l3、u2-l5 分别精读过）：

- **normal（标准）模式**：一次通信是一个**内核家族协作**——`get_dispatch_layout` 先算本地路由布局，`notify_dispatch`/`cached_notify` 做传输前握手（首次与非首次），`dispatch` 发送、`combine` 聚合。内核毫秒级，独占一条通信流。
- **low-latency（`_ll`）模式**：只剩 `dispatch_ll`/`combine_ll` 两个内核。内核把 RDMA 消息**发出后立刻退出**（释放全部 SM），等待被推迟到计算结束之后。内核微秒级，与计算同流。

两代内核解决的是同一个问题的两种约束：**批量越大、通信越重，越值得用专门的流与完整的握手协议；批量越小、时延越敏感，越要把通信压缩成"发出即忘"**。

#### 4.2.2 核心流程

统计通信内核族的方法（承接 u3-l1 的聚合思路）：

1. 取 `cat=="kernel" and ph=="X"` 的事件；
2. 按名称前缀分族：`dpsk::ep::internode::` 前缀（排除 `cdpsk::ep::` 这种 CUB 假匹配——`contains("dpsk::ep::")` 会把 `cdpsk::ep::DeviceScan...` 也匹配进来，prefill 里有 16 个这样的干扰项）；
3. 计数、总时长、占全部 kernel 时长的比例、所在 stream。

占比只在 `kernel` 类别内计算（u3-l1 的口径规则：CPU 墙钟不可与 GPU 执行混算）。

#### 4.2.3 源码精读

train.json 中一条完整的 normal `dispatch` 内核事件（本讲所有通信统计的样本）：

- [train.json:L31324-L31337](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31324-L31337)：`dpsk::ep::internode::dispatch<false, 8, true, 8>` 完成事件。关键字段：`tid: 27` 即通信流（stream 27）；`dur: 3320`（微秒，毫秒级）；`blocks per SM: 0.15`、`est. achieved occupancy %: 4`——内核只占极少量 SM；签名里的 `dpsk::ep::internode::SourceMeta*` 是接收端元信息缓冲（u2-l3 讲过）。

prefill.json 中同族的 `dispatch`（模板参数不同）：

- [prefill.json:L175934-L175947](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L175934-L175947)：`dpsk::ep::internode::dispatch<false, 4, false, 4>`，`tid: 16`（prefill 的通信流是 stream 16），`dur: 5189` 微秒，`blocks per SM: 0.18`——与 train 版同为毫秒级、低 SM 占用的形态。

decode.json 中的 `dispatch_ll`（单行文件，行号不适用，见 [decode.json:L1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1)）：名称为 `void dpsk::ep::internode::dispatch_ll<true, 3, 10, 7168>`，在 stream 7 上与计算内核同流，单次时长呈双峰（约 17µs 的发送段与 60~80µs 的等待段，u2-l5）。

三份轨迹的通信内核族实测汇总：

| 指标 | train.json | prefill.json | decode.json |
|---|---|---|---|
| DeepEP 内核数 | 36 | 580 | 470 |
| DeepEP 总时长 | 113.44 ms | 1799.49 ms | 17.05 ms |
| **占 kernel 总时长比例** | **46.4%** | **48.0%** | **18.9%** |
| 所在 stream | 27（独立通信流） | 16（独立通信流） | 7（与计算同流） |
| 家族构成 | dispatch 8、combine 8、cached_notify 12、notify_dispatch 4、get_dispatch_layout 4 | 每类恰 116 个（5 类） | dispatch_ll 235、combine_ll 235 |
| 单内核典型时长 | 毫秒级（如 3320µs） | 毫秒级（如 5189µs；combine 均值约 8.7ms） | 微秒级（发送 ~17µs / 等待 60~80µs） |
| 通信流忙时 | 114.52 ms | 1799.49 ms | ——（无独立流） |

三个值得咀嚼的形态差异：

1. **train 与 prefill 的占比几乎相同（46.4% vs 48.0%）但绝对量差 16 倍**（113ms vs 1799ms）——训练 chunk 与预填充都处在"通信重到必须遮挡"的区间，只是窗口长度不同。
2. **train 的家族成员数是 4 的倍数**（每类 4/8/12 个），因为每个 chunk 含 4 个 MoE 层（u2-l2）；**prefill 每类恰 116 个 = 58 个 MoE 层 × 2 微批**（u2-l4）；**decode 的 235 = 58 层 × 2 微批 × 约 2 步余数**（116 槽稳态，u2-l6）。计数本身就是结构指纹。
3. **decode 完全没有 normal 内核、也没有独立通信流**——EP128 世界更大但单步通信只有 17ms，不值得一条专职流；同时 CUDA Graph 回放要求流拓扑固定，同流的 `_ll` 内核天然适配（3437/3647 个内核共享 correlation 7077）。

另注意 train 的通信流 stream 27 上还有 4 个 `per_token_cast_to_fp8` 量化内核（实测），提醒我们"按流分类"在该场景下不完全等价于"按功能分类"——4.4 节的重叠率计算对此做了说明。

#### 4.2.4 代码实践

**实践目标**：复现上表的前三行（家族计数、总时长、占比），并亲眼看到 `cdpsk` 假匹配陷阱。

**操作步骤**（示例代码）：

```python
# compare_deepep.py —— 三份轨迹的 DeepEP 家族统计（示例代码）
import json
from collections import Counter

for f in ["train.json", "prefill.json", "decode.json"]:
    ev = json.load(open(f))["traceEvents"]
    kernels = [e for e in ev if e.get("cat") == "kernel" and e.get("ph") == "X"]
    total = sum(e["dur"] for e in kernels)
    # 精确匹配 internode/cuda 命名空间，避免 cdpsk::ep:: 假阳性
    comm = [e for e in kernels if "dpsk::ep::" in e["name"]
            and not e["name"].lstrip("void ").startswith("cdpsk")]
    names = Counter(e["name"].split("(")[0].replace("void ", "")
                    .split("<")[0] for e in comm)
    print(f, f"n={len(comm)} ms={sum(e['dur'] for e in comm)/1e3:.2f}"
          f" pct={100*sum(e['dur'] for e in comm)/total:.1f}")
    for n, c in names.most_common():
        print(f"   {c:4d}x {n}")
```

**需要观察的现象**：train 输出 6 个家族名、合计 36；prefill 输出 5 个家族名、每类 116、合计 580；decode 只输出 `dispatch_ll` 与 `combine_ll` 各 235。若把过滤条件写成简单的 `contains("dpsk::ep::")`，prefill 会多出 16 个 `cdpsk::ep::` 的 CUB 扫描内核（合计约 596 个，时长几乎不变，但家族表被污染）。

**预期结果**：与 4.2.3 表格一致（46.4% / 48.0% / 18.9%）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 prefill 的 `combine` 均值（约 8.67ms，116 次共 1006.18ms）明显大于 `dispatch` 均值（约 4.59ms，116 次共 532.86ms）？

**答案**：dispatch 发出的是 FP8 量化的激活（每 token 按 Top-K 路由发往少量专家，且 u2-l4 观察到每次 dispatch 前都重算布局）；combine 要把专家输出以 `__nv_bfloat16` 原路聚回源端并按门控权重求和，数据量与聚合操作都更重。内核模板参数也可见一斑：prefill 的 combine 是 `combine<false, 4, __nv_bfloat16, 4, 4, 16, 24>`。

**练习 2**：decode 用 EP128（世界最大），为什么反而是通信占比最低（18.9%）的场景？

**答案**：解码每步每 GPU 只有 128 个 request、每 request 1 个 token，参与 all-toall 的数据量极小；占比低的分母效应同样重要——解码窗口短（kernel 总时长仅 89.78ms），且 split-KV 注意力这类访存内核占掉了 34.9% 的时间（见 4.3）。

**练习 3**：如果只在轨迹里搜索 `dispatch` 这个子串，会漏掉或多算什么？

**答案**：会漏掉 `combine`/`notify_dispatch`/`cached_notify`/`get_dispatch_layout`（多算方面，`notify_dispatch` 含 `dispatch` 子串所以不会漏，但 `cdpsk` 扫描内核不含该子串不受影响）；更稳妥的做法是按 `dpsk::ep::` 命名空间分族、再按 `<` 前的短名聚合——这正是 u3-l1 引入"名称归一化"的原因。

### 4.3 注意力内核族对比：compute_attn_ws vs flash_fwd_splitkv_mla

#### 4.3.1 概念说明

三份轨迹的注意力内核构成三张不同的面孔：

- **train**：前向用 `flash::compute_attn_ws`（workspace 版 FlashAttention 前向），反向用 `flash::FlashAttnBwd`（cutlass 风格长名）——训练独有反向。
- **prefill**：只有 `flash::compute_attn_ws`——与 train 前向同款，因为预填充就是"长序列前向"。
- **decode**：`flash::flash_fwd_splitkv_mla_kernel` + `flash_fwd_splitkv_mla_combine_kernel` 两段式——序列方向很长（4K KV 缓存）、查询方向极短（每 token 1 个查询），必须把 KV 切片分给多个块并行、再合并部分结果（split-KV 策略，u2-l6）。

两代内核的本质区别是**工作负载**：预填充是"许多查询 × 许多键"的大矩阵注意力，一次扫完；解码是"极少查询 × 很长键"，瓶颈从算力转为访存，split-KV 用并行度换延迟。

#### 4.3.2 核心流程

注意力占比的算法与通信族相同，只是分族关键字换成 `compute_attn_ws` / `FlashAttnBwd` / `flash_fwd_splitkv_mla`。

\[
\text{注意力占比} = \frac{\sum_{k \in \text{注意力族}} \mathrm{dur}(k)}{\sum_{k \in \text{kernel}} \mathrm{dur}(k)} \times 100\%
\]

#### 4.3.3 源码精读

- [prefill.json:L179286](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L179286)：prefill 第一条 `flash::compute_attn_ws` 内核事件，模板参数 `Flash_fwd_kernel_traits<192, 128, 128, 12, 2, ...>`——192 头维相关的 tile 形状，是 MLA 预填充注意力的标准配置（u2-l4）。
- [train.json:L41542](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L41542)：train 的 `flash::FlashAttnBwd` 内核（长模板名，`CollectiveMainloopBwd`）——训练轨迹独有的反向注意力。
- decode.json（单行文件，行号不适用）：`flash::flash_fwd_splitkv_mla_kernel<Flash_fwd_kernel_traits_mla<576, 64, 64, 8, ...>>` 主内核（实测 120 次共 29.94ms，单次约 240~250µs）与 `flash_fwd_splitkv_mla_combine_kernel<..., 512, 64>` 合并内核（120 次共 1.36ms，单次约 11µs）。

三份轨迹的注意力族实测汇总：

| 指标 | train.json | prefill.json | decode.json |
|---|---|---|---|
| 注意力内核总数 | 8 | 122 | 240 |
| 注意力总时长 | 20.05 ms | 342.59 ms | 31.30 ms |
| **占 kernel 总时长比例** | **8.2%** | **9.1%** | **34.9%** |
| 家族构成 | compute_attn_ws 4（4.94ms）+ FlashAttnBwd 4（15.11ms） | compute_attn_ws 122（342.59ms） | splitkv_mla 120（29.94ms）+ combine 120（1.36ms） |
| 单内核典型时长 | 前向约 1.2ms / 反向约 3.8ms | 约 2.8ms | 主内核约 249µs / 合并约 11µs |
| 场景 Top-1 内核 | internode::combine（49.44ms） | internode::combine（1006.18ms） | **flash_fwd_splitkv_mla（29.94ms）** |

占比从 8~9% 跳到 34.9% 不是解码注意力"变慢了"，而是**负载画像变了**：预填充窗口里充满大 GEMM（`fp8_gemm_kernel` 家族实测合计超过 1.1s），注意力只是众多大块之一；解码没有大 GEMM 可摊（最大的 GEMM 仅 9.22ms），访存型的 split-KV 注意力自然成为时间线主角。Top-1 内核的更替（combine → combine → splitkv_mla）是三个场景最直观的指纹差异。

#### 4.3.4 代码实践

**实践目标**：验证"解码的注意力是第一大家族，预填充/训练的第一大家族是通信"。

**操作步骤**（示例代码）：

```python
# compare_attn.py —— 注意力族占比 + 全场 Top-3 内核（示例代码）
import json
from collections import defaultdict

ATTN_KEYS = ("compute_attn_ws", "FlashAttnBwd", "flash_fwd_splitkv_mla")

for f in ["train.json", "prefill.json", "decode.json"]:
    ev = json.load(open(f))["traceEvents"]
    kernels = [e for e in ev if e.get("cat") == "kernel" and e.get("ph") == "X"]
    total = sum(e["dur"] for e in kernels)
    agg = defaultdict(lambda: [0, 0])          # 短名 -> [次数, 总时长]
    for e in kernels:
        short = e["name"].split("(")[0].split("<")[0].replace("void ", "")
        agg[short][0] += 1
        agg[short][1] += e["dur"]
    attn = sum(v[1] for k, v in agg.items() if any(a in k for a in ATTN_KEYS))
    print(f, f"attn pct = {100*attn/total:.1f}%")
    for k, (n, d) in sorted(agg.items(), key=lambda x: -x[1][1])[:3]:
        print(f"   {n:4d}x {d/1e3:8.2f}ms  {k[:60]}")
```

**需要观察的现象**：三份文件的第一名分别是 `dpsk::ep::internode::combine`、`dpsk::ep::internode::combine`、`flash::flash_fwd_splitkv_mla_kernel`；decode 的 Top-3 里通信内核退居第 2、第 7。

**预期结果**：注意力占比 8.2% / 9.1% / 34.9%；decode 的 splitkv_mla 家族登顶。

#### 4.3.5 小练习与答案

**练习 1**：为什么 train 的注意力族里有 `FlashAttnBwd` 而另两份没有？

**答案**：训练需要反向传播，注意力反向是独立内核（[train.json:L41542](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L41542)）；推理（预填充/解码）只做前向，永远不会有 Bwd/W 类内核——这也是 u2-l2 区分 F/B/W 注解时讲过的训练/推理差异在内核层的投影。

**练习 2**：decode 的注意力为什么需要 split-KV，而 prefill 不需要？

**答案**：预填充的查询很长，单个 tile 就有足够的并行度填满 132 个 SM；解码每步每 request 只有 1 个查询，若不切 KV，一个请求的注意力只能用一个块算、其余 SM 闲置。split-KV 把 4K 的 KV 序列切成多段并行累积 online softmax 部分结果，再用 combine 内核归并（合并公式与两段式时序见 u2-l6），本质是用块间并行换延迟。

**练习 3**：只用"注意力占比"这一项，能否区分一份未知轨迹来自解码还是预填充？

**答案**：能给出强先验但不能定论：占比高于约 30% 且存在 `splitkv_mla` 内核 → 解码；占比约 9% 且存在 `compute_attn_ws` → 预填充或训练前向。但严谨的判据还是内核名本身与 `world_size`（32/128）的组合——单指标给指纹，多指标才给结论。

### 4.4 统一指标横向汇总：三张重叠率答卷

#### 4.4.1 概念说明

u3-l2 定义的重叠率口径是：把通信与计算的事件区间分别合并成忙时集合 \(C\) 与 \(P\)，计算交集：

\[
\text{通信重叠率} = \frac{|C \cap P|}{|C|}, \qquad
\text{计算被覆盖率} = \frac{|C \cap P|}{|P|}
\]

其中 \(|{\cdot}|\) 表示区间总长度（微秒）。这个口径在 train 与 prefill 上度量"双流真并行"；但 decode 的通信与计算同在 stream 7，区间交集恒为 0——**这不是"没有重叠"，而是重叠发生在 SM 资源层面而非时间线层面**。decode 的正确账本是 u3-l2 的三段式分解：

\[
T_{\text{窗口}} \approx T_{\text{计算}} + T_{\text{发送}} + T_{\text{等待}} + T_{\text{气泡}}
\]

`dispatch_ll` 内核的 dur 里同时装着发送段（约 17µs，发完即释放 SM）与等待段（60~80µs，SM 已让给另一微批的计算）。所以横向对比要同时报告"区间口径的重叠率"与"机制描述"两列，否则 decode 会被误读。

#### 4.4.2 核心流程

三份轨迹统一执行（u3-l2 的方法，本讲实测复核）：

1. 通信集 \(C\)：train 取 stream 27 的内核区间；prefill 取 stream 16；decode 取名称含 `dispatch_ll`/`combine_ll` 的内核区间（**按名称而非按流**，u2-l5 的教训）。
2. 计算集 \(P\)：train 取 stream 7+23；prefill 取 stream 7；decode 取其余全部内核区间。
3. 各自区间排序合并，再求交集长度。

#### 4.4.3 源码精读

三份轨迹的重叠率实测（本讲用区间合并 + 求交复核，方法与 u3-l2 一致）：

| 指标 | train.json | prefill.json | decode.json |
|---|---|---|---|
| 通信忙时 \(|C|\) | 114.52 ms | 1799.49 ms | 17.05 ms |
| 计算忙时 \(|P|\) | 124.25 ms | 1929.55 ms | 72.72 ms |
| 交集 \(|C \cap P|\) | 110.27 ms | 1641.35 ms | **0 ms** |
| 通信重叠率 | **96.2%** | **91.2%** | 0%（区间口径失效） |
| 计算被覆盖率 | 88.7% | 85.0% | —— |
| 实际重叠机制 | 双流并行：通信流 stream 27 与计算流 stream 7/23 时间上相交 | 双流并行：通信流 stream 16 与计算流 stream 7 相交 | SM 级延迟隐藏：发送 ~17µs → 计算 ~105µs → 等待 60~80µs（u2-l5 实测三段式） |

把 4.1~4.4 的所有实测拼成最终全景对比表：

| 维度 | 指标 | train（EP64） | prefill（EP32） | decode（EP128） |
|---|---|---|---|---|
| 规模 | traceEvents | 14240 | 85422 | 19417 |
| 规模 | kernel 数 / 总时长 | 963 / 244.04ms | 4111 / 3749.29ms | 3647 / 89.78ms |
| 流 | 计算流 | stream 7（907）/23（16） | stream 7（3525） | stream 7（3642） |
| 流 | 通信流 | stream 27（40） | stream 16（580） | 无（同 stream 7） |
| 流 | 其他 | —— | stream 13（6，NCCL） | stream 13（3）/16（2） |
| 通信 | DeepEP 数量/占比 | 36 / 46.4% | 580 / 48.0% | 470 / 18.9% |
| 通信 | 内核代际 | normal 全家族 | normal 全家族 | low-latency 两件套 |
| 通信 | 区间口径重叠率 | 96.2% | 91.2% | 0（SM 级隐藏） |
| 注意力 | 家族/占比 | ws+Bwd / 8.2% | ws / 9.1% | splitkv / 34.9% |
| 调度 | 机制 | DualPipe 前反向分块 | 双微批 | 双微批 + CUDA Graph |
| 调度 | 调度注解 | 42 条（1F1B 等） | 无（6 条 NCCL 自动注解） | 无（3 条 NCCL 自动注解） |

三个层面的读表结论（也是综合实践的参考答案骨架）：

1. **通信占比相近的 train/prefill 用"独立流 + 完整握手"**：46~48% 的通信时间若裸奔会直接吞掉一半吞吐，所以两场景都专职一条通信流、靠 DualPipe（96.2%）或双微批（91.2%）把通信几乎完全藏进计算。
2. **decode 换了一整套武器**：通信绝对量小（17ms）且单步只有约 724µs（u2-l6 的槽节拍），独立流与毫秒级 normal 内核都太重；`_ll` 内核把通信压成微秒级"发出即走"，叠加 CUDA Graph 消除启动开销，让等待期的 SM 全部让给另一微批——重叠从"流级"降到"SM 资源级"，区间交集自然为 0。
3. **EP 规模与策略的关系**：EP128 世界最大但单 token 通信量最小；EP64/EP32 通信重、值得宏观调度去遮挡。**规模本身不决定策略，通信量与时延预算才决定**。

#### 4.4.4 代码实践

**实践目标**：一键产出上面那张全景对比表。

**操作步骤**（示例代码，整合 u3-l1 聚合与 u3-l2 区间运算）：

```python
# compare_all.py —— 三轨迹统一指标对比表（示例代码）
import json
from collections import defaultdict

def merge(iv):                       # iv: [(s,e), ...] -> 合并后的不相交区间
    iv = sorted(iv)
    out = []
    for s, e in iv:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out

def inter_len(a, b):                 # 两个已合并区间列表的交集总长
    i = j = total = 0
    while i < len(a) and j < len(b):
        s, e = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if s < e: total += e - s
        if a[i][1] < b[j][1]: i += 1
        else: j += 1
    return total

SPEC = {                              # 每份文件的通信集/计算集划分
    "train.json":   (lambda e: e["args"]["stream"] == 27,
                     lambda e: e["args"]["stream"] in (7, 23)),
    "prefill.json": (lambda e: e["args"]["stream"] == 16,
                     lambda e: e["args"]["stream"] == 7),
    "decode.json":  (lambda e: "_ll" in e["name"],
                     lambda e: "_ll" not in e["name"]),
}

rows = []
for f, (is_comm, is_comp) in SPEC.items():
    ev = json.load(open(f))["traceEvents"]
    k = [e for e in ev if e.get("cat") == "kernel" and e.get("ph") == "X"]
    kd = sum(e["dur"] for e in k)
    comm = [e for e in k if is_comm(e)]
    comp = [e for e in k if is_comp(e)]
    deepep = [e for e in k if "dpsk::ep::" in e["name"]
              and "cdpsk" not in e["name"]]
    C = merge([(e["ts"], e["ts"] + e["dur"]) for e in comm])
    P = merge([(e["ts"], e["ts"] + e["dur"]) for e in comp])
    ov = inter_len(C, P)
    rows.append({
        "file": f, "world": json.load(open(f))["distributedInfo"]["world_size"],
        "kernels": len(k), "k_ms": round(kd / 1e3, 2),
        "deepep": len(deepep), "deepep_pct": round(100 * sum(e["dur"] for e in deepep) / kd, 1),
        "comm_ms": round(sum(e["dur"] for e in comm) / 1e3, 2),
        "overlap_pct": round(100 * ov / sum(e - s for s, e in C), 1) if C else None,
    })
for r in rows:
    print(r)
```

**需要观察的现象**：`train.json` 行的 `overlap_pct` 接近 96，`prefill.json` 接近 91，`decode.json` 为 0——但它的 `deepep_pct` 只有约 19、`comm_ms` 只有约 17。三个数字放在一起，就是"decode 不是没重叠，而是换了重叠方式"的证据链。

**预期结果**：与 4.4.3 的表逐项一致。若 train 的通信重叠率明显偏低，检查是否把 stream 23（辅助计算流）从计算集里漏掉了。

#### 4.4.5 小练习与答案

**练习 1**：train 的"通信重叠率 96.2%"与"计算被覆盖率 88.7%"为什么可以都不等于 100%，且两者不相等？

**答案**：分子相同（交集 110.27ms）而分母不同（通信忙时 114.52ms、计算忙时 124.25ms）。通信有 4.25ms 暴露在外（无计算同时进行），计算有 13.98ms 没有通信陪伴（这部分本来也不需要陪伴——纯计算时段不算浪费）。两个指标回答两个不同问题：通信暴露是浪费，计算独占是正常。

**练习 2**：如果给 decode 也强行按"stream 划分通信集"，会得到什么错误结论？

**答案**：decode 的 `dispatch_ll`/`combine_ll` 就在 stream 7 上，按流划分会把通信内核划进计算集，得到"通信集为空、重叠率无定义"或"通信即计算"的荒谬结果。正确做法是按内核名称划分（u2-l5 实测教训，本讲 SPEC 中 decode 的判据用 `"_ll" in e["name"]`）。

**练习 3**：三份轨迹的 DeepEP 内核数量（36/580/470）能否直接比较"谁通信更频繁"？

**答案**：不能直接比——分母不同（MoE 层数 × 微批数 × 采集窗口内的调度轮数各不相同）。可比的是**每层的内核构成**：train 每 MoE 层一组 normal 家族（约 9 个内核/层，含握手与布局），prefill 同为每层一组（5 类 × 116 = 58 层 × 2 微批），decode 每层每微批只剩 2 个 `_ll` 内核。代际演进的方向是"每层通信内核数越来越少、越来越轻"。

## 5. 综合实践

把本讲全部脚本合并成一个小工具 `compare_traces.py`，产出一张最终对比表并用三到五句结论解释三个场景的通信策略取舍。

**任务要求**：

1. **输入**：仓库根目录的 `train.json`、`prefill.json`、`decode.json`。
2. **输出表列**（至少）：场景、`world_size`、有内核的 GPU 进程数（应为 1）、内核总数、DeepEP 通信内核族及占比、注意力内核族及占比、通信-计算重叠率（注明 decode 用区间口径为 0 的原因）、Top-1 内核名。
3. **实现要点**：
   - decode.json 是单行大文件，必须 `json.load` 整体解析（u3-l1 的教训）；
   - DeepEP 分族要排除 `cdpsk::ep::` 假匹配（4.2 的陷阱）；
   - 通信集划分：train 按流 27、prefill 按流 16、decode 按名称 `_ll`（4.4 的口径）；
   - 重叠率用区间合并 + 双指针求交（4.4.4 的 `merge`/`inter_len`）。
4. **结论**：基于你自己的数字，写三到五句话回答——训练、预填充、解码各自用什么机制遮挡通信？为什么 decode 敢让区间交集为 0？

**参考结论**（可对照你自己的表述）：

> 训练（EP64）通信占内核时间 46.4%，靠 DualPipe 把前向与反向 chunk 拆成可交错的碎块、配合独立通信流 stream 27，把 96.2% 的通信藏进计算；预填充（EP32）通信占 48.0%，形态与训练同族（normal 内核 + 独立流 16），但遮挡机制换成双微批——一个微批通信时另一个微批计算，重叠率 91.2%。解码（EP128）单步仅约 724µs、通信总共 17ms，养不起独立流与毫秒级内核，改用 `dispatch_ll`/`combine_ll` 把 RDMA 消息发出后立刻释放全部 SM，等待期正好填入另一微批的计算，再叠加 CUDA Graph 固定流拓扑、消除启动开销——重叠从"流级并行"降为"SM 资源级延迟隐藏"，因此区间交集为 0 却依然高效。三个场景共同说明：策略选择由通信绝对量与时延预算决定，而不是由集群规模决定。

**验证方式**：把你表中每个数字与本讲 4.1.3、4.2.3、4.3.3、4.4.3 各表的实测值对照；一致即通过（数字来自本讲编写时的 `jq` 实测，你的运行环境若与仓库版本不同，以你本地结果为准并检查仓库 HEAD 是否仍为 `4496024`）。

## 6. 本讲小结

- 三份轨迹的配置有三个可信来源且互为补充：README（场景与策略）、`distributedInfo`（world_size=64/32/128 即 EP 数）、`traceName`（train/prefill 编码了实验细节，decode 没有）；采集口径差异（窗口长度、注解习惯、模拟均衡路由）决定了只能比占比与形态，不能比绝对时长。
- 通信内核族呈两代形态：train/prefill 用 normal 全家族（布局 + 握手 + 收发），占比 46.4%/48.0%、毫秒级、独立通信流（stream 27/16）；decode 用 low-latency 两件套 `dispatch_ll`/`combine_ll`，占比 18.9%、微秒级、与计算同流。
- 注意力族占比 8.2% → 9.1% → 34.9% 的跳变来自负载画像：预填充/训练被大 GEMM 摊薄，解码没有大 GEMM、访存型 split-KV MLA 成为 Top-1 内核；Top-1 内核从 combine 更替为 splitkv_mla 是最快的场景指纹。
- 统一指标下的重叠率答卷：train 96.2%、prefill 91.2%（双流区间相交），decode 区间交集为 0 但以"发送-填隙-等待"三段式在 SM 资源级隐藏延迟——口径必须与机制一起报告。
- 横向分析的方法论：先统一口径（占比只在 kernel 类内算、通信集划分 train/prefill 按流而 decode 按名称），再比占比与形态，最后用机制解释数字——这套流程对任意 PyTorch Chrome Trace 都适用。

## 7. 下一步学习建议

本讲之后，第三单元只剩最后一讲 u3-l4（自建 profiling 与生态项目连接）。建议：

1. **先做 u3-l4 的实践**：用 `torch.profiler.profile(activities=[CPU, CUDA])` + `export_chrome_trace` 录制你自己的模型，导出后直接用本讲的 `compare_traces.py` 分析——检验这套工具在"非 DeepSeek 轨迹"上的通用性，并观察 `distributedInfo` 与 `traceName` 在你自己导出的文件里长什么样。
2. **顺着内核名走出本仓库**：把 `dpsk::ep::internode::dispatch_ll`、`flash_fwd_splitkv_mla_kernel`、`dpsk::gemm::fp8_gemm_kernel` 三个名字分别带到 [DeepEP](https://github.com/deepseek-ai/DeepEP)、[DualPipe](https://github.com/deepseek-ai/DualPipe)、[DeepGEMM](https://github.com/deepseek-ai/DeepGEMM) 仓库中对照源码，理解轨迹里的形态差异背后的实现差异。
3. **延伸阅读**：DeepSeek-V3 技术报告中关于通信-计算重叠与无辅助损失负载均衡的章节，能把"为什么要模拟绝对均衡路由"这一采集前提放回完整语境。
