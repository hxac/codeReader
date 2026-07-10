# 跑通第一个构建命令

## 1. 本讲目标

前几讲我们看清了 LLM2FPGA 的目标（u1-l1）、仓库结构（u1-l2）和 Nix 工具链（u1-l3），但都还停留在「读」的层面。本讲我们要第一次真正「跑」起来：

学完本讲，你应当能够：

- 执行 README 给出的 gate（验收）命令，把 TinyStories-1M 全流程跑成一个构建产物；
- 读懂目标名 `tiny-stories-1m-baseline-float-selftest-all-memory-utilization` 每一段的含义；
- 解释 `result` 符号链接指向什么、为什么 `result/summary.txt` 能直接被读取；
- 打开 `summary.txt`，找到 `clb_luts` 这一行，判断设计是否装得下目标 FPGA；
- 说清楚为什么这个报告是「Yosys 估算」而不是「nextpnr 布局布线报告」。

---

## 2. 前置知识

在动手前，先用三句话回顾几个本讲会反复用到的概念（细节见 u1-l1 / u1-l3）：

- **Nix flake 与派生（derivation）**：`flake.nix` 里每个 `packages.<名字>` 都是一个「派生」，`nix build .#<名字>` 会把它构建出来。Nix 会把构建结果放进 `/nix/store/` 下一个带哈希的目录，并在你当前目录留一个 `result` 符号链接指向它。
- **降级（lowering）链**：LLM2FPGA 把 PyTorch 模型一步步降级成硬件，链路是 `PyTorch → torch-MLIR → CIRCT → SystemVerilog → Yosys(RTLIL)`。本讲构建命令会触发这条链一直走到 Yosys 这一段。
- **CLB LUT**：FPGA 里的基础查找表（Look-Up Table），是衡量「设计有多大」最常用的指标。目标芯片 XC7K480T 总共有 298,600 个 CLB LUT。

> 关键认知（承接 u1-l1）：当前 TinyStories-1M 的 baseline-float 流水线**能跑通降级**，但设计所需的 LUT 数远超芯片容量（约 141 倍）。所以这个构建命令产出的不是一个能烧进 FPGA 的比特流，而是一份「瓶颈报告」。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md) | 给出 gate 命令、目标名分解、期望产物文件清单和最关键的数字。 |
| [deliverables/3e-tiny-stories-1m-resource-report.md](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md) | Task 3 的资源与瓶颈报告，逐项解释命令做了什么、为什么不是 nextpnr 报告、141 倍结论。 |
| [flake.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix) | 把 gate 命令的目标名映射到一条真实的派生依赖链，并定义目标 FPGA 的容量 `fpgaCapacities`。 |
| [scripts/pipeline/write_utilization_report.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py) | 真正生成 `summary.txt` / `summary.json` / `stat.json` 的脚本，决定了报告里每一行的格式与含义。 |

---

## 4. 核心概念与源码讲解

本讲的三个最小模块是：① `nix build` 与 `result` 符号链接；② 目标命名约定；③ 三类产物 `summary.txt` / `summary.json` / `stat.json`。

### 4.1 `nix build` 与 `result` 符号链接

#### 4.1.1 概念说明

`nix build .#<目标名>` 是 Nix flake 的标准用法：`.` 表示「当前目录的 flake」，`.#<目标名>` 表示「这个 flake 的 outputs 里 `packages` 下的某个属性」。它做两件事：

1. 解析依赖图，把目标及其所有上游派生按顺序构建（命中缓存就跳过）；
2. 构建完成后，在**调用目录**下创建一个名为 `result` 的符号链接，指向产物在 `/nix/store/` 里的真实路径。

因此你看到的 `result` 不是一个普通文件夹，而是一个「软指针」。`cat result/summary.txt` 之所以能工作，是因为这个指针指向的 store 目录里恰好有一个 `summary.txt`。

#### 4.1.2 核心流程

gate 命令的执行流程可以概括为：

```text
nix build .#tiny-stories-1m-...-utilization -L
   │
   ├── 1. 解析 flake.nix 的 packages.tiny-stories-1m-...-utilization
   ├── 2. 按依赖链构建：工具链(CIRCT/torch-MLIR/Yosys) → 模型降级 → RTLIL
   ├── 3. 套上自测外壳、外部化超大存储、分阶段 synth_xilinx 映射 → design JSON
   ├── 4. 用 write_utilization_report.py 从 JSON 统计 → summary.{txt,json} + stat.json
   └── 5. 在当前目录建 result 符号链接 → 指向上面那个产物目录
```

`-L` 是 `--print-build-logs` 的缩写：把每个派生的构建日志实时打到终端。对于这种长流程构建，它能让你看到「现在卡在哪一步」，否则你只会盯着一个空白进度条。

#### 4.1.3 源码精读

gate 命令本身写在 README 和 3e 报告里，二者完全一致：

[README.md:74-77](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L74-L77) 给出要执行的命令；紧接其后的 [README.md:79-82](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L79-L82) 用一句话说明了它做了什么：降级到 RTL/Yosys 形式、套上 TinyStories 自测外壳、估算资源、写出 `result` 符号链接。

那么 `.#tiny-stories-1m-baseline-float-selftest-all-memory-utilization` 在 flake 里到底指向什么？看 [flake.nix:796-798](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L796-L798)：

```nix
tiny-stories-1m-baseline-float-selftest-all-memory-utilization =
  tinyStories1mBaselineFloatSelftestAllMemory.utilizationReport;
```

也就是说，这个目标名 = `tinyStories1mBaselineFloatSelftestAllMemory` 这个 bundle 的 `.utilizationReport` 字段。而该 bundle 由 [flake.nix:672-678](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L672-L678) 构造，关键点：`topName = "tiny_stories_selftest_top"`、`modelIl = tinyStories1mBaselineFloatIl`、`capacities = fpgaCapacities`。

真正写出 `result` 目录里那三个文件的是 `mkMappedJsonUtilizationReport`，见 [flake.nix:531-549](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L531-L549)。它先 `mkdir -p "$out"`，再调用 `write_utilization_report.py` 把 `--summary-txt`、`--summary-json`、`--stat-json` 都写到 `$out` 下。因为 `runCommand "${name}-utilization"` 的输出名以 `-utilization` 结尾，所以 store 路径形如 `/nix/store/<哈希>-tiny-stories-1m-baseline-float-selftest-all-memory-utilization/`，而 `result` 就指向它——这正是 `result/summary.txt` 能被读取的原因。

> 提示：这一节不需要你记派生名，只要记住一句话——**目标名最终落到一个目录型派生上，`result` 是指向它的符号链接，目录里就是三份报告文件**。

#### 4.1.4 代码实践

1. **实践目标**：亲手看到 `result` 符号链接的真实指向，理解它不是普通目录。
2. **操作步骤**（在仓库根目录执行）：
   ```bash
   # 先不构建，只问 Nix「这个目标会产出什么」（解析，不执行）：
   nix build .#tiny-stories-1m-baseline-float-selftest-all-memory-utilization --dry-run
   ```
3. **观察现象**：`--dry-run` 会列出「将要构建」和「将从缓存拉取」的派生路径。注意其中有一个以 `-tiny-stories-1m-baseline-float-selftest-all-memory-utilization` 结尾的 store 路径。
4. **预期结果**：你会在输出里看到这个目标对应的派生；真正 `nix build` 后，`readlink result`（或 `ls -l result`）会显示它指向上面那个 store 路径。
5. **待本地验证**：完整 `nix build` 首次会编译整条工具链（CIRCT / torch-MLIR / Yosys），耗时可能很长且占用大量内存；本步骤仅用 `--dry-run` 即可在数秒内验证目标解析与产物路径，无需真正完成构建。

#### 4.1.5 小练习与答案

**练习 1**：为什么命令里要带 `-L`？去掉会怎样？
**答案**：`-L` 即 `--print-build-logs`，实时打印每个派生的日志。去掉后 Nix 默认只在出错时才显示日志，长流程构建时你会看不到当前进度。

**练习 2**：`result` 删了会丢失构建产物吗？
**答案**：不会。`result` 只是指向 `/nix/store/` 里某个路径的符号链接，删掉它只是断开指针；真正的产物仍在 store 中（直到被 `nix gc` 回收）。

---

### 4.2 目标命名约定

#### 4.2.1 概念说明

目标名 `tiny-stories-1m-baseline-float-selftest-all-memory-utilization` 看起来很长，但它是**用连字符把若干「开关」串起来**的复合名。每一段都对应流水线里的一个独立选择，读懂这五段就读懂了「这条命令到底跑了哪种配置」。这种命名风格在 LLM2FPGA 里是通用的：把模型、变体、后处理选项直接编码进目标名，避免再传一堆命令行参数。

#### 4.2.2 核心流程

五段含义对照表（来自 README 的分解）：

| 段 | 含义 |
| --- | --- |
| `tiny-stories-1m` | 被测模型：roneneldan/TinyStories-1M，项目能找到的最小 LLM。 |
| `baseline-float` | 浮点基线模型，**未做量化**（对比未来可能的 int8/int4 变体）。 |
| `selftest` | 把设计包进一个顶层自测外壳（`tiny_stories_selftest_top`），使其成为一个有清晰顶层的完整设计。 |
| `all-memory` | 把所有「超大」Handshake 存储模块 blackbox 掉，当作外部存储候选（阈值见下）。 |
| `utilization` | 只产出 Yosys 资源估算，**不出比特流**。 |

#### 4.2.3 源码精读

README 的官方分解在 [README.md:83-92](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L83-L92)；3e 报告里有一份几乎相同但更详细的版本，见 [deliverables/3e-tiny-stories-1m-resource-report.md:27-38](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md#L27-L38)。

其中两段需要特别展开：

- `all-memory`：3e 报告说明「oversized 指**每个 Handshake 存储模块至少 128 kbit**」，并指出 Handshake 方言是当前流水线最大的资源负担之一，去掉它是 Task 6 的目标。在 flake 里这个阈值就是 `mkTinyStoriesSelftestBundle` 的 `externalMemoryMinModuleBits ? (128 * 1024)`，见 [flake.nix:566-567](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L566-L567)（128 × 1024 = 131072 bit = 128 kbit）。
- `utilization`：它对应 `mkTinyStoriesSelftestBundle` 返回结构里的 `utilizationReport` 字段（而非 `stages`），见 [flake.nix:602-606](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L602-L606)。这条 bundle 同时算了 `stages`（分阶段综合的中间 IL/JSON）和 `utilizationReport`（最终报告）；目标名末尾的 `utilization` 就是选择了后者对外暴露。

> 命名约定的工程价值：同一套流水线可以通过改名字段产生不同产物（如未来可能有 `...-int8-...-bitstream`），而无需改动命令结构。

#### 4.2.4 代码实践

1. **实践目标**：从源码确认 `all-memory` 的 128 kbit 阈值，并理解它如何被传下去。
2. **操作步骤**：
   - 在 [flake.nix:566-586](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L566-L586) 找到 `mkTinyStoriesSelftestBundle` 的参数 `externalMemoryMinModuleBits ? (128 * 1024)`，并跟踪它如何作为 `minModuleBits` 传给 `mkExternalizedMemoryPlan`。
   - 对照 3e 报告 [deliverables/3e-tiny-stories-1m-resource-report.md:33-36](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md#L33-L36) 的文字说明。
3. **观察现象**：你会看到默认阈值 128×1024 bit，且这个值最终传到 `externalize_large_memories.py` 的 `--min-module-bits`。
4. **预期结果**：能复述「`all-memory` = 把每个 ≥128 kbit 的 Handshake 存储当外部存储」这一映射关系。
5. **待本地验证**：若想确认被外部化的具体模块，需运行构建后查看 `mkExternalizedMemoryPlan` 产物中的 `report.json`（本讲不展开，留给 u6-l2）。

#### 4.2.5 小练习与答案

**练习 1**：如果把末段从 `utilization` 换成（假设的）`bitstream`，产物会变成什么？
**答案**：会走另一条派生——经过 nextpnr 布局布线和 `fasm2frames`/`xc7frames2bit` 产出 `.bit` 比特流（参考 matmul 模型的 `mkBitstream`）。本目标特意选 `utilization`，说明当前阶段只关心资源够不够，不打算真的烧板子。

**练习 2**：`baseline-float` 这一段暗示未来可能出现哪种对照变体？
**答案**：量化变体，例如 `int8` / `int4`。它们会换掉浮点算子，预期大幅降低 LUT/DSP 用量——这正是 Task 6 资源最小化的候选方向之一（见 u7-l3）。

---

### 4.3 三类产物：`summary.txt` / `summary.json` / `stat.json`

#### 4.3.1 概念说明

构建完成后，`result` 目录下固定有三个文件，分别面向不同读者：

- **`summary.txt`**：给人看的纯文本资源摘要，最关键的一行是 `clb_luts`。
- **`summary.json`**：同样的摘要内容，但写成 JSON，给脚本/CI 处理。
- **`stat.json`**：更底层的 Yosys 叶单元（leaf cell）逐类型计数，是生成上面两份摘要的「原料」。

三者的数据流向是单向的：Yosys 的 mapped design JSON →（`write_utilization_report.py` 统计）→ `stat.json` + `summary.json` + `summary.txt`。

#### 4.3.2 核心流程

`write_utilization_report.py` 递归展开模块层次，把顶层 `tiny_stories_selftest_top` 里所有「叶单元」按类型数出来，再分类归并：

\[ \text{资源} = \text{分类归并}\bigl(\text{递归统计}(\text{顶层模块的所有叶单元})\bigr) \]

关键的几个归类规则（在脚本顶部定义）：

- **clb_luts**：所有匹配 `LUT[1-6]` / `LUT6_2` / `CFGLUT5` 的单元求和。
- **clb_ffs**：触发器类型集合 `{FDCE, FDPE, FDRE, FDSE, LDCE, LDPE}`。
- **dsp**：`{DSP48E1}`。
- **bram36 / bram18**：`{FIFO36E1, RAMB36E1}` / `{FIFO18E1, RAMB18E1}`。
- **slices_lower_bound**：slices 的下界估计，取 LUT 和 FF 两者的上界：

\[ \text{slices\_lower\_bound} = \max\left(\left\lceil\frac{\text{luts}}{8}\right\rceil,\ \left\lceil\frac{\text{ffs}}{8}\right\rceil\right) \]

（每片 slice 最多容纳 8 个 LUT 和 8 个 FF，所以除以 8 取上界。）

每一类资源都会算一个百分比：`pct = 100 * used / capacity`，其中 capacity 来自 flake 里的 `fpgaCapacities`。

#### 4.3.3 源码精读

报告里各行的展示顺序由 [write_utilization_report.py:21](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py#L21) 的 `SUMMARY_ORDER` 决定：

```python
SUMMARY_ORDER = ["slices_lower_bound", "clb_luts", "clb_ffs", "dsp", "bram36", "bram36_equiv", "bram_kb"]
```

每一行的格式由 `summary_text` 生成，见 [write_utilization_report.py:125-135](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py#L125-L135)：每行形如 `- <key>: <used> / <capacity> (<pct>%)`。

各资源的 used 值怎么算，见 [write_utilization_report.py:97-110](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py#L97-L110)：其中 `luts` 用正则 `LUT_RE` 匹配（[第 15 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py#L15)），`slices_lower_bound` 在 [第 102 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py#L102) 按上面的公式取 max。

capacity 这一侧的数字来自 flake 的 `fpgaCapacities`，见 [flake.nix:249-256](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L249-L256)，XC7K480T 的容量是：

```nix
fpgaCapacities = {
  slices = 74650;
  clb_luts = 298600;
  clb_ffs = 597200;
  dsp = 1920;
  bram36 = 955;
  bram_kb = 34380;
};
```

> 这套容量数字的出处：flake 注释引用了 AMD 7-series 产品选型手册（[flake.nix:248](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L248) 指向 docs.amd.com 的 7-series-product-selection-guide，查 XC7K480T）。

根据上述脚本逻辑，`result/summary.txt` 的格式（示例，具体数值待本地验证，`clb_luts` 数字取自 README 的报告）大致是：

```text
top: tiny_stories_selftest_top
estimated mapped resource usage:
- slices_lower_bound: <待本地验证> / 74650 (<待本地验证>%)
- clb_luts: 42123250 / 298600 (14106.92%)
- clb_ffs: <待本地验证> / 597200 (<待本地验证>%)
- dsp: <待本地验证> / 1920 (<待本地验证>%)
- bram36: <待本地验证> / 955 (<待本地验证>%)
- bram36_equiv: <待本地验证> / 955 (<待本地验证>%)
- bram_kb: <待本地验证> / 34380 (<待本地验证>%)
largest leaf cell types:
- <类型>: <数量>
...
```

其中 `clb_luts` 的 used 取 README 报告的 42,123,250。超配倍数为：

\[ \text{超配倍数} = \frac{42123250}{298600} \approx 141.07 \text{（即约 141 倍）} \]

百分比 `pct = 100 × 42123250 / 298600 ≈ 14106.92%`。

#### 4.3.4 代码实践：为什么这不是 nextpnr 的 PnR 报告

这是本讲最重要的实践（也是 gate 任务的核心）。我们要把「读到的数字」和「报告性质」讲清楚。

1. **实践目标**：从 `summary.txt` 读出 `clb_luts` 的 used 与 capacity，算出超配倍数，并论证它为何不是布局布线报告。
2. **操作步骤**（重型，可选执行）：
   ```bash
   nix build .#tiny-stories-1m-baseline-float-selftest-all-memory-utilization -L
   cat result/summary.txt
   ```
3. **需要观察的现象**：
   - 找到 `clb_luts:` 这一行，记下 `used`（约 42123250）和 `capacity`（298600）。
   - 注意文件第二行写的是 `estimated mapped resource usage:`（关键词 **estimated**、**mapped**），而不是 "placed and routed"。
4. **预期结果**：
   - 超配倍数 ≈ 141（used / capacity）。
   - `pct` 一列远超 100%（约 14106.92%）。
5. **为什么不是 nextpnr 的 PnR 报告？** 三个理由，均来自 3e 报告 [deliverables/3e-tiny-stories-1m-resource-report.md:59-61](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md#L59-L61) 与 [deliverables/3e-tiny-stories-1m-resource-report.md:88-96](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md#L88-L96)：
     1. **数据来源不同**：这份报告来自 Yosys `synth_xilinx` 后的 mapped JSON（统计 LUT/FF/DSP/BRAM 叶单元数量），而真正的 PnR 报告要由 nextpnr-xilinx 在布局布线后产出。
     2. **nextpnr 跑不动**：作者尝试过用 nextpnr-xilinx 做这条路线，但**每次都内存溢出（OOM）**，没能产出任何 PnR 结果。
     3. **OOM 是合理的**：设计比目标芯片大约 141 倍，而 XC7K480T 已是本项目支持的最大 FPGA 系列；指望 nextpnr 在内存里装下并布通一个 141 倍超大的设计本就不现实，所以退而求其次用 Yosys 估算当「瓶颈报告」。
6. **待本地验证**：完整构建耗时与内存消耗很大；若本地无法完成，可直接基于上面 README/3e 的权威数字完成「读数 + 推理」部分（这也是 gate 任务真正考察的能力）。

> 轻量替代实践（若完整构建太重，可立即做）：不跑构建，直接读 [write_utilization_report.py:97-135](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py#L97-L135)，手动用 `clb_luts=42123250`、`capacity=298600` 代入 `row()` 与 `summary_text()`，预测 `summary.txt` 中 `clb_luts` 这一行的确切文字，再与 README 的 141 倍结论相互印证。

#### 4.3.5 小练习与答案

**练习 1**：`summary.json` 和 `summary.txt` 内容上有什么关系？为什么要同时给两份？
**答案**：内容等价，只是格式不同——`summary.txt` 给人读，`summary.json` 给脚本/CI 解析（如自动判断是否超配）。见 [write_utilization_report.py:150-156](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py#L150-L156)。

**练习 2**：`stat.json` 与 `summary.json` 的关键区别是什么？
**答案**：`stat.json` 记录的是**逐类型叶单元计数**（含每个子模块的 direct 与 leaf 计数，见 [write_utilization_report.py:157-168](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py#L157-L168)），是更底层、更详细的数据；`summary.json` 是把这些计数**分类归并**后得到的资源摘要。前者是原料，后者是成品。

**练习 3**：为什么报告里既有 `bram36` 又有 `bram36_equiv`？
**答案**：`bram36` 只数 36K 的 BRAM；`bram36_equiv` 把 18K 的 BRAM 折半后换算成「等价 36K 数量」(`bram36 + bram18/2`)，便于与芯片的 36K 容量直接比较（见 [第 108 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py#L108)）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「**读数 → 算账 → 下结论**」的完整资源瓶颈分析：

1. **解析目标**：把 `tiny-stories-1m-baseline-float-selftest-all-memory-utilization` 拆成五段，写出每段含义，并指出哪一段决定了「这份产物是报告而不是比特流」。
2. **读数**：打开 `result/summary.txt`（或基于 README/3e 的权威数字），记录 `clb_luts` 与 `slices_lower_bound` 两行的 `used / capacity / pct`。
3. **算账**：用 `clb_luts` 的 used ÷ capacity 算超配倍数，验证它约为 141；再解释为什么 `pct` 列会显示成 14106.92% 这种「怪数字」。
4. **下结论**：用三句话说明——
   - 这个设计能不能装进 XC7K480T？
   - 这份报告的可信度边界在哪（是 Yosys 估算而非 PnR，为什么）？
   - 按此结论，项目的下一步应该做什么（提示：Task 6 资源最小化，而不是 Task 4 上板）？

> 验收标准：你能不查文档说出 `clb_luts` 的 capacity（298600）、超配倍数（约 141）、报告性质（Yosys 估算、nextpnr OOM），并指出 `utilization` 这一段决定了产物是报告。

---

## 6. 本讲小结

- gate 命令 `nix build .#tiny-stories-1m-baseline-float-selftest-all-memory-utilization -L` 会触发整条降级链直到 Yosys，并在当前目录留下 `result` 符号链接。
- `result` 指向 `/nix/store/` 里一个以 `-utilization` 结尾的目录型派生，目录内就是三份报告文件。
- 目标名是「模型 + 变体 + 开关」的复合，五段分别表示模型、浮点基线、自测外壳、外部化超大存储、产出资源报告。
- `summary.txt`（人读）/ `summary.json`（脚本读）/ `stat.json`（叶单元原料）由 `write_utilization_report.py` 递归统计 mapped JSON 生成。
- 判断装不装得下的关键指标是 `clb_luts`：used ≈ 42,123,250，capacity = 298,600，超配约 141 倍。
- 这是一份 Yosys 估算而非 nextpnr PnR 报告——因为 nextpnr-xilinx 在这条路线上 OOM，且设计比芯片大 141 倍。

---

## 7. 下一步学习建议

本讲你已能把整条流水线「一键跑通」并读懂瓶颈报告。接下来建议：

- **进入前端**：学习 [scripts/compile-pytorch.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py) 与适配器契约（u2-l1、u2-l2），看清降级链最起点「PyTorch 图是怎么导出的」。
- **理解资源报告的下游**：若你想知道那份 mapped JSON 是怎么一步步综合出来的，可跳读 [flake.nix:386-472](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L386-L472) 的 `mkSynthJsonStages`（u5-l4 会详细讲）。
- **把瓶颈读深**：直接精读 [deliverables/3e-tiny-stories-1m-resource-report.md](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md)，它是本讲所有结论的权威来源，也是后续 u6（专家实战）与 u7-l3（路线图）的起点。
