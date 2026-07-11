# 资源利用报告：从 JSON 到容量对比

## 1. 本讲目标

上一讲（u5-l2）把 matmul 跑到了可烧录的 `.bit` 比特流；但对体积大 141 倍的 TinyStories-1M，nextpnr-xilinx 直接 OOM，根本走不到布局布线。这时我们仍然要回答一个关键工程问题：**这个设计到底比目标芯片大多少？大在哪种资源上？** ——没有这个数字，就无法判断「下一步该优化什么」。

本讲讲解 LLM2FPGA 如何在**不做物理实现**的前提下，仅凭 Yosys 综合出的 mapped JSON 估算 FPGA 资源用量。核心是 `scripts/pipeline/write_utilization_report.py` 这个独立 Python 脚本，它完成三件事：

1. 递归展开模块层次，把顶层设计里所有**叶单元（leaf cell）**数清楚。
2. 用一组正则/集合把叶单元归类为 LUT、FF、DSP、BRAM。
3. 与 `flake.nix` 里 `fpgaCapacities`（XC7K480T 的容量）对比，算出占用百分比，产出 `summary.txt` / `summary.json` / `stat.json`。

学完后你应该能够：

- 读懂 `leaf_counts` 的递归 + memo + 环检测写法，并能手动模拟一次小例子的展开。
- 说出 `LUT_RE`、`FF_TYPES` 等分类规则分别匹配哪些 Xilinx 7 系原语。
- 解释 `slices_lower_bound` 为什么是 `max(⌈luts/8⌉, ⌈ffs/8⌉)`。
- 用 `fpgaCapacities.clb_luts = 298600` 复算出「超配约 141 倍」这个结论。

## 2. 前置知识

- **叶单元（leaf cell）**：综合后不再包含子模块的最底层硬件原语，例如一个 6 输入查找表 `LUT6`、一个 D 触发器 `FDRE`、一个 DSP 块 `DSP48E1`。它们是 FPGA 资源计量的「原子」。
- **mapped JSON**：Yosys 的 `write_json` 把综合后的网表写成 JSON，结构大致是 `{modules: {模块名: {cells: {实例名: {type: ...}}}}}`。本讲的输入就是这种 JSON。
- **CLB / Slice / LUT / FF**：Xilinx 7 系列 FPGA 的基本逻辑单元是 **CLB（Configurable Logic Block）**，每个 CLB 在 7 系里通常拆成 **Slice**，每个 Slice 含 **8 个 LUT** 和 **8 个 FF**。所以 LUT 和 FF 的数量除以 8 就能给出 Slice 用量的下界。
- **DSP / BRAM**：硬宏资源。DSP 是乘加单元（`DSP48E1`），BRAM 是块存储（36 Kbit 的 `RAMB36E1`、18 Kbit 的 `RAMB18E1`），它们独立于 CLB，用一块就少一块。
- **容量（capacity）**：目标芯片各种资源的总数上限，是百分比的分母。本讲里它来自 `flake.nix` 的 `fpgaCapacities`，对应 Kintex-7 **XC7K480T**。

> 与上一讲的区别：u5-l2 关心「能不能产出比特流」；本讲关心「在设计装不下、跑不到 PnR 时，怎么估出它有多大」。这是一份 **Yosys 估算**，不是 nextpnr 布局布线报告。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [scripts/pipeline/write_utilization_report.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py) | 本讲主角。读 mapped JSON → 递归数叶单元 → 归类 → 对比容量 → 写三份产物。 |
| [flake.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix) | 提供 `fpgaCapacities`（芯片容量）、`mkMappedJsonUtilizationReport`（把脚本包成 Nix 派生）与最终 package 绑定。 |
| [deliverables/3e-tiny-stories-1m-resource-report.md](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md) | 给出「超配约 141 倍」「nextpnr OOM」「下一步是 Task 6」的工程结论，是本讲数字的权威出处。 |

## 4. 核心概念与源码讲解

### 4.1 递归叶单元统计

#### 4.1.1 概念说明

Yosys 综合后的网表是一棵**模块层次树**：顶层 `main` 里实例化了若干子模块，子模块里又可能是别的子模块或最终叶单元。要做资源核算，必须把这棵树**拍平（flatten）**到叶单元层：顶层用了多少个 `LUT6`、多少个 `FDRE`、多少个 `DSP48E1`……

为什么不能直接信任 Yosys 的 `stat` 命令？因为 `stat` 是过程性摘要，且在分层网表里只报「直接」单元数，不一定递归到底；另外项目在 staged 综合流程（见 u5-l4）里，stage9 的 `write_json` 之前并未对全设计做 `flatten`，层次可能仍然存在。`write_utilization_report.py` 因此自己写了一份**带 memo 与环检测的递归**，确保无论层次是否被拍平都能数对。

#### 4.1.2 核心流程

设 `modules` 是「模块名 → 模块内容」的字典，`name` 是当前要展开的模块名：

```text
leaf_counts(modules, name, memo, stack):
  若 name 已在 memo：直接返回缓存值        # 避免重复展开
  若 name 在 stack：报「层次环」错误        # 防御自反/互相实例化
  若 name 不在 modules：报「找不到模块」

  把 name 压入 stack
  counts = 空
  对当前模块的每个直接单元 (cell_type, 实例数 instances)：
      若 cell_type 不是 modules 里的模块名（即是叶原语）：
          counts[cell_type] += instances           # 叶单元，直接计数
      否则（cell_type 是子模块）：
          leaves = leaf_counts(..., cell_type, ...) # 递归展开子模块
          对 leaves 里每个 (leaf_type, 每实例叶数)：
              counts[leaf_type] += instances * 每实例叶数   # 按实例数倍乘
  把 name 移出 stack
  memo[name] = counts
  return counts
```

关键数学关系：若顶层实例化了 \(k\) 个子模块 \(M\)，而 \(M\) 内含 \(n\) 个 `LUT6`，则顶层摊到的 `LUT6` 数是 \(k \cdot n\)。递归把这一乘法层层向上传递。

#### 4.1.3 源码精读

直接单元计数由 `cell_counts` 完成，它把模块里的 `cells` 字典聚合成 `{类型: 实例数}`：

[write_utilization_report.py:46-51](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py#L46-L51) —— 遍历 `module["cells"]`，按 `type` 字段累加，返回一个 `Counter`。

递归主体在 `leaf_counts`，三重防御 + 倍乘逻辑都集中在这里：

[write_utilization_report.py:54-78](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py#L54-L78) —— `memo` 做记忆化、`stack` 做环检测、`instances * leaves_per_instance` 做按实例数倍乘。注意第 70 行的 `if cell_type not in modules` 是「叶 vs 子模块」的判别式：一个 cell 的 `type` 如果恰好是 `modules` 里的某个键，它就被当成子模块实例去递归；否则就是叶原语，直接计入。

`main` 以顶层模块名为起点启动一次递归，并把 memo 留着复用：

[write_utilization_report.py:145-148](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py#L145-L148) —— `leaf_counts(modules, args.top, memo, set())` 得到 `top_counts`，这是顶层设计摊到的全部叶单元计数，后续归类与百分比都基于它。

#### 4.1.4 代码实践

**实践目标**：手动模拟递归，确认你理解「按实例数倍乘」。

**操作步骤**（源码阅读型实践，无需运行）：

1. 想象一个极小的 mapped JSON，`modules` 只有两个模块：
   - `pe`：含 2 个 `LUT6`、1 个 `FDRE`。
   - `main`：实例化了 3 个 `pe`（即 3 个 cell，其 `type` 都是 `pe`），自己没有别的叶单元。
2. 在纸上模拟 `leaf_counts(modules, "main", {}, set())`：
   - 进入 `main`，`cell_counts` 得到 `{pe: 3}`。
   - `pe` 在 `modules` 里 → 递归 `leaf_counts(..., "pe", ...)`，返回 `{LUT6: 2, FDRE: 1}`。
   - 倍乘：`LUT6 += 3*2 = 6`，`FDRE += 3*1 = 3`。

**需要观察的现象**：顶层 `main` 最终的叶单元计数应为 `{LUT6: 6, FDRE: 3}`。

**预期结果**：`6` 个 `LUT6`、`3` 个 `FDRE`。若你得到 `{pe: 3}`，说明你忘了在 `cell_type in modules` 时递归，把子模块当成了叶原语。

**待本地验证**：可选地，你可以写一个 5 行的 Python 字典喂给真实的 `leaf_counts` 函数验证（在 `nix develop` 里 `python3 -c "..."` 即可）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `stack` 那段环检测删掉，遇到模块 `A` 实例化 `B`、`B` 又实例化 `A` 的设计会怎样？

**参考答案**：会无限递归，直到 Python 触发 `RecursionError`（栈溢出）。`stack` 用集合记录「当前递归路径上正在展开的模块」，一旦再次遇到同一模块名就立即抛 `SystemExit`，给出清晰的 `module hierarchy cycle at ...` 报错，而不是默默栈溢出。

**练习 2**：`memo` 在这里除了「加速」，还隐含保证了什么正确性？

**参考答案**：同一子模块无论被多少个上层实例化，它的叶单元展开结果只算一次（存进 memo），再按实例数倍乘复用。这既避免了重复计算，也保证了「同一个子模块的展开口径全局一致」——不会因为从不同父模块进入而算出不同的叶数。

---

### 4.2 资源类型分类正则

#### 4.2.1 概念说明

递归数清叶单元后，得到的是一个像 `{LUT6: 42102600, FDRE: 98000000, DSP48E1: 12, RAMB36E1: 0, ...}` 的「大杂烩」。但资源报告要回答的是「LUT 用了多少、FF 用了多少、DSP 用了多少、BRAM 用了多少」，所以需要把每种叶原语**归类**到对应的资源桶。

Xilinx 7 系原语名是固定约定的：查找表叫 `LUT1`…`LUT6`、`LUT6_2`、`CFGLUT5`；触发器叫 `FDCE`/`FDPE`/`FDRE`/`FDSE`（带时钟使能/复位）和锁存器 `LDCE`/`LDPE`；DSP 是 `DSP48E1`；36 Kbit 块存储是 `RAMB36E1`/`FIFO36E1`，18 Kbit 是 `RAMB18E1`/`FIFO18E1`。

脚本用「正则」匹配 LUT、用「集合」匹配其余三类，这是有意的区分：LUT 名字是一个**模式族**（`LUT` 后跟 1–6 的数字，外加两个变体），适合用正则；其余原语是**有限可枚举**的精确名字，集合查找更快也更醒目。

#### 4.2.2 核心流程

归类逻辑全部集中在 `summarize` 里，借助一个闭包 `count(types)`：

```text
count(types) = counts 里所有「cell_type 属于 types 集合」的实例数之和

luts   = counts 里所有匹配 LUT_RE 的 cell_type 实例数之和
ffs    = count(FF_TYPES)
bram36 = count(BRAM36_TYPES)
bram18 = count(BRAM18_TYPES)
```

LUT 用正则、其余用集合，是两类分类规则的并置：

- **正则规则**：`LUT_RE = ^(LUT[1-6]|LUT6_2|CFGLUT5)$`，匹配 `LUT1`…`LUT6`、`LUT6_2`、`CFGLUT5`。
- **集合规则**：`FF_TYPES = {FDCE, FDPE, FDRE, FDSE, LDCE, LDPE}`、`DSP_TYPES = {DSP48E1}`、`BRAM36_TYPES = {FIFO36E1, RAMB36E1}`、`BRAM18_TYPES = {FIFO18E1, RAMB18E1}`。

#### 4.2.3 源码精读

五个分类常量在文件顶部一字排开：

[write_utilization_report.py:15-19](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py#L15-L19) —— `LUT_RE` 是正则；`FF_TYPES`、`DSP_TYPES`、`BRAM36_TYPES`、`BRAM18_TYPES` 是集合。

归类与求和发生在 `summarize` 内部：

[write_utilization_report.py:97-110](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py#L97-L110) —— `luts` 走 `LUT_RE.match`（第 97 行），其余三类走 `count(...)`（第 98–100 行）。`bram36_equiv` 把 18 Kbit 块折半算成 36 Kbit 当量：`bram36 + bram18 / 2.0`；`bram_kb` 则按真实容量 `bram36*36 + bram18*18` 求总 Kbit 数。这两个派生量是为了和容量做不同口径的对比。

注意：`count` 的内部实现（第 86–87 行）是「`cell_type in types`」，对集合是 O(1) 查找；而 LUT 走的是正则 `match`。两者风格不同但目的统一：把整份叶单元计数控映射到「设计用了多少 LUT/FF/DSP/BRAM」。

#### 4.2.4 代码实践

**实践目标**：验证分类规则覆盖了 7 系常用原语，并理解 `bram36_equiv` 的折算。

**操作步骤**：

1. 在 `nix develop` 里启动 `python3`，把正则和集合原样敲进去：
   ```python
   import re
   LUT_RE = re.compile(r"^(LUT[1-6]|LUT6_2|CFGLUT5)$")
   LUT_RE.match("LUT6")   # 应返回 Match
   LUT_RE.match("LUT7")   # 应返回 None —— 7 系没有 LUT7，正则故意只到 6
   LUT_RE.match("LUT6_2") # 应返回 Match
   ```
2. 手算：若某设计有 4 个 `RAMB36E1` 和 3 个 `RAMB18E1`，`bram36_equiv` 与 `bram_kb` 各为多少？

**需要观察的现象**：`LUT7` 被正则拒绝；`LUT6_2` 被接受。

**预期结果**：
- `bram36_equiv = 4 + 3/2 = 5.5`（36 Kbit 当量块数）。
- `bram_kb = 4*36 + 3*18 = 144 + 54 = 198`（Kbit）。

**待本地验证**：上述 Python 交互可在 `nix develop` 中直接复现。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `LUT_RE` 写成 `LUT[1-6]` 而不是 `LUT\d` 或 `LUT[0-9]`？

**参考答案**：Xilinx 7 系的查找表最高就是 6 输入，不存在 `LUT7`、`LUT8`。用 `[1-6]` 既是如实反映硬件，也起到白名单作用——若综合意外抖出一个 `LUT7`（理论上不该有），它不会被误计入 LUT 桶，便于发现异常。`LUT0` 同理不存在，故从 `1` 起。

**练习 2**：`bram36_equiv = bram36 + bram18 / 2.0` 这个折半的依据是什么？

**参考答案**：一个 36 Kbit 块（`RAMB36E1`）的容量恰好等于两个 18 Kbit 块（`RAMB18E1`）。所以把 18 Kbit 块数除以 2，就把所有块存储统一折算成「36 Kbit 当量块数」，便于和芯片的 `bram36` 容量直接比。

---

### 4.3 容量对比与百分比

#### 4.3.1 概念说明

有了「设计用了多少 LUT/FF/DSP/BRAM」，还差一个分母才能回答「装不装得下」。分母就是目标芯片的**容量**。在 LLM2FPGA 里，目标芯片固定为 Kintex-7 **XC7K480T**，其容量写死在 `flake.nix` 的 `fpgaCapacities` 里，作为整个资源报告的基准。

百分比的计算本身很简单：\( \text{pct} = 100 \times \text{used} / \text{capacity} \)。但有两个工程细节值得讲清：

1. **Slice 是 LUT/FF 的派生下界**：Slice 容量对比用的不是直接统计值，而是 `slices_lower_bound`。因为一个 Slice 装得下 8 个 LUT **和** 8 个 FF，所以最少需要 \( \max(\lceil \text{luts}/8\rceil, \lceil \text{ffs}/8\rceil) \) 个 Slice。
2. **容量与容量的注入**：`fpgaCapacities` 在 Nix 侧定义，通过命令行参数 `--capacity-*` 注入 Python 脚本；脚本本身不知道芯片型号，只认参数。这种「数据 vs 逻辑」分离让脚本可复用于任意芯片。

#### 4.3.2 核心流程

```text
对每种资源 key（slices_lower_bound、clb_luts、clb_ffs、dsp、bram36、bram36_equiv、bram_kb）：
    used      = 设计用量（来自 leaf_counts → 归类）
    capacity  = 命令行 --capacity-* 传入的芯片容量
    pct       = capacity>0 ? round(100*used/capacity, 2) : 0
    行 = {used, capacity, pct}

其中 slices_lower_bound = max( ⌈luts/8⌉, ⌈ffs/8⌉ )
```

数学上，Slice 下界取两个约束的较大值：

\[
\text{slices\_lower\_bound} = \max\!\left(\left\lceil \frac{\text{luts}}{8} \right\rceil,\ \left\lceil \frac{\text{ffs}}{8} \right\rceil\right)
\]

理由是每个 Slice 同时拥有 8 个 LUT 与 8 个 FF 位置，LUT 侧和 FF 侧各自给出一个不可违背的下界，实际 Slice 用量必须同时满足两者，故取 `max`。它只是**下界**——真实布局常因 LUT/FF 配对、进位链、布线拥塞而需要更多 Slice。

#### 4.3.3 源码精读

容量定义在 `flake.nix`，附带了 AMD 官方产品手册的出处注释：

[flake.nix:248-256](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L248-L256) —— `fpgaCapacities` 列出 `slices / clb_luts / clb_ffs / dsp / bram36 / bram_kb` 六项容量。其中 `clb_luts = 298600`，这就是「超配 141 倍」的分母来源。注释里的 AMD 文档链接是这块数字的溯源。

`pct` 与 `row` 的计算在 `summarize` 的闭包里：

[write_utilization_report.py:89-95](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py#L89-L95) —— `capacity = getattr(args, CAPACITY[key])` 通过 `CAPACITY` 映射把资源 key 翻译成命令行参数名（如 `clb_luts → capacity_clb_luts`），`pct` 在 `capacity <= 0` 时兜底为 0，避免除零。

`CAPACITY` 这张映射表把「资源 key」与「命令行参数名」一一对应：

[write_utilization_report.py:21-30](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py#L21-L30) —— `SUMMARY_ORDER` 决定报告里各资源的输出顺序，`CAPACITY` 决定每项去哪个参数取容量；注意 `bram36_equiv` 与 `bram36` 共用 `capacity_bram36` 容量（因为前者是后者的当量折算）。

容量注入与三份产物写入发生在 Nix 派生 `mkMappedJsonUtilizationReport`：

[flake.nix:531-549](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L531-L549) —— 把 `capacities.slices` 等六个值经 `toString` 插进 `--capacity-*` 参数，调用脚本，把 `summary.json` / `summary.txt` / `stat.json` 写进 `$out`。

最终这个派生被绑成一个可构建的 package：

[flake.nix:796-797](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L796-L797) —— `tiny-stories-1m-baseline-float-selftest-all-memory-utilization` 就是 `mkTinyStoriesSelftestBundle` 里的 `utilizationReport`（见 [flake.nix:602-604](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L602-L604)）。这正是 u1-l4 里那条 gate 命令的产物。

> 完整结论见 [deliverables/3e-tiny-stories-1m-resource-report.md:76-77](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md#L76-L77)：设计「在 LUT 维度上比目标 FPGA 大约 141 倍」，因此下一步是 Task 6（资源最小化）而非 Task 4（上板）。

#### 4.3.4 代码实践

**实践目标**：完成规格指定的两项——解释 `slices_lower_bound` 的 `max` 公式，并从 `fpgaCapacities` 复算 XC7K480T 的 `clb_luts` 容量与 141 倍结论。

**操作步骤**：

1. 打开 [write_utilization_report.py:102](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py#L102)，找到：
   ```python
   "slices_lower_bound": max(math.ceil(luts / 8), math.ceil(ffs / 8)),
   ```
2. 回答：为什么是 `max`，而不是 `min`、不是 `luts/8 + ffs/8`、不是 `(luts+ffs)/8`？
3. 打开 [flake.nix:251](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L251)，读出 `clb_luts = 298600`。
4. 用这个分母验证 141 倍：\( 298600 \times 141 \approx 42{,}102{,}600 \)。即 TinyStories-1M 的 LUT 用量约 4210 万量级。

**需要观察的现象**：

- `slices_lower_bound` 同时受 LUT 与 FF 两侧约束，是两者的**较大**下界。
- 298,600 × 141 ≈ 4210 万，与「设计约 4210 万 CLB LUT」的量级吻合。

**预期结果**：

- **关于 `max`**：一个 7 系 Slice 同时含 8 个 LUT 位与 8 个 FF 位。LUT 侧要求至少 \( \lceil \text{luts}/8\rceil \) 个 Slice，FF 侧要求至少 \( \lceil \text{ffs}/8\rceil \) 个 Slice。这两个是各自独立的「硬下界」，实际 Slice 数必须**同时**满足，所以取较大者 `max`。若取 `min` 会违反较多一侧的约束；若相加或求和再除 8 则高估了「LUT 与 FF 可共享同一 Slice」的事实——它们本来就是同一个 Slice 的两面，不能重复计数。
- **关于容量**：XC7K480T 的 `clb_luts` 容量 = **298,600**。141 倍即约 **4210 万 LUT**，远超芯片容量，故设计装不下，且 nextpnr-xilinx 在此规模 OOM（见 3e 报告）。

**待本地验证**：执行 `nix build .#tiny-stories-1m-baseline-float-selftest-all-memory-utilization -L` 后，`cat result/summary.txt` 中 `clb_luts` 行的 `used` 字段应落在 4210 万量级（具体尾数以本地构建结果为准）。

#### 4.3.5 小练习与答案

**练习 1**：某设计用了 100 个 LUT、20 个 FF，`slices_lower_bound` 是多少？若用了 100 个 LUT、10000 个 FF，又是多少？

**参考答案**：
- 情形一：\( \max(\lceil 100/8\rceil, \lceil 20/8\rceil) = \max(13, 3) = 13 \) 个 Slice。LUT 是瓶颈。
- 情形二：\( \max(\lceil 100/8\rceil, \lceil 10000/8\rceil) = \max(13, 1250) = 1250 \) 个 Slice。FF 成了瓶颈。这正说明为什么要取 `max`——哪一侧紧张由谁说了算。

**练习 2**：若把目标芯片换成容量更小的 Spartan，资源报告脚本需要改哪里？

**参考答案**：脚本本身**不用改**。容量是经 `--capacity-*` 参数注入的（见 `mkMappedJsonUtilizationReport`）。只需在 Nix 侧把传入的 `capacities` 换成新芯片的 `fpgaCapacities`（即改 `flake.nix` 里那个属性集），脚本会自动用新分母算百分比。这正是「数据 vs 逻辑」分离的好处。

**练习 3**：`pct` 在 `capacity <= 0` 时返回 0（第 94 行）。这是在防什么？

**参考答案**：防止除零异常。如果某项容量参数漏传或被显式置 0，`100.0 * used / capacity` 会抛 `ZeroDivisionError`，让整个报告失败；兜底成 0 让脚本仍能产出报告（该项显示 0%），把问题留给人去读而不是硬中断。

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「假想芯片」的资源核算。

**任务**：假设你为一个虚构的小芯片做资源报告，它的容量是 `slices=5000, clb_luts=40000, clb_ffs=80000, dsp=100, bram36=100, bram_kb=3600`。一个设计经 `leaf_counts` 得到顶层叶单元计数：

```text
LUT6: 30000, LUT5: 5000, FDRE: 70000, FDCE: 5000,
DSP48E1: 40, RAMB36E1: 30, RAMB18E1: 20
```

请：

1. 算出 `luts`、`ffs`、`bram36`、`bram18`、`bram36_equiv`、`bram_kb`、`slices_lower_bound` 七个值。
2. 对 `clb_luts`、`clb_ffs`、`dsp`、`bram36` 四项分别算出占用百分比。
3. 判断这个设计装不装得下，瓶颈在哪种资源。

**参考答案**：

1. `luts = 30000 + 5000 = 35000`（`LUT6`、`LUT5` 都匹配 `LUT_RE`）；`ffs = 70000 + 5000 = 75000`（`FDRE`、`FDCE` ∈ `FF_TYPES`）；`bram36 = 30`；`bram18 = 20`；`bram36_equiv = 30 + 20/2 = 40`；`bram_kb = 30*36 + 20*18 = 1080 + 360 = 1440`；`slices_lower_bound = max(⌈35000/8⌉, ⌈75000/8⌉) = max(4375, 9375) = 9375`。
2. `clb_luts: 35000/40000 = 87.5%`；`clb_ffs: 75000/80000 = 93.75%`；`dsp: 40/100 = 40%`；`bram36: 30/100 = 30%`。
3. **装不下**：FF 侧 93.75% 看似还剩一点，但 Slice 下界 `9375 > 5000`（容量），即 Slice 维度早已爆掉（`9375/5000 = 187.5%`）；LUT 侧 87.5% 也接近上限。瓶颈是 **Slice**（由 FF 主导），其次是 LUT。这个例子正好演示了 `slices_lower_bound` 为何不可省——只看 LUT/FF 百分比会误判。

## 6. 本讲小结

- `leaf_counts` 用**递归 + memo + stack 环检测**把分层网表拍平到叶单元层，并通过 `instances * leaves_per_instance` 按实例数倍乘，得到顶层设计的全量叶单元计数。
- 资源分类用两套规则并存：`LUT_RE` 正则匹配 `LUT1`–`LUT6` / `LUT6_2` / `CFGLUT5`；`FF_TYPES` / `DSP_TYPES` / `BRAM36_TYPES` / `BRAM18_TYPES` 用集合精确枚举其余原语。
- `slices_lower_bound = max(⌈luts/8⌉, ⌈ffs/8⌉)`，因为一个 7 系 Slice 同时含 8 LUT 与 8 FF，两侧各自给出硬下界，取较大者；它只是下界，不是真实 PnR 后的 Slice 数。
- 容量来自 `flake.nix` 的 `fpgaCapacities`（XC7K480T），经 `--capacity-*` 命令行参数注入脚本，脚本本身与芯片型号解耦——换芯片只需改 Nix 侧的属性集。
- 产出三份文件：`summary.txt`（人读）、`summary.json`（脚本读）、`stat.json`（每个模块的直接/叶单元计数原料）。
- 这份报告是 **Yosys 估算**而非 nextpnr PnR 报告，因为设计比 XC7K480T 大约 141 倍（`clb_luts` 容量 298,600），nextpnr-xilinx 直接 OOM。

## 7. 下一步学习建议

- **下一讲 u5-l4（分阶段 Yosys 综合与 targeted memory_map）** 会回到 `flake.nix`，讲解 `mkSynthJsonStages` 怎样把 `synth_xilinx` 拆成 9 个可观察 stage，以及本讲消费的那份 mapped JSON（stage9 产物）到底经历了哪些中间形态。本讲只读了 JSON，下一讲讲 JSON 是怎么一步步综合出来的。
- **横向回顾 u4-l1/u4-l2**：仿真那条线以 PyTorch 为黄金参考验证「语义正确」；本讲这条线验证「资源够不够」。两条线共同回答「这个降级链产出的硬件既对、又有多大」。
- **后续 u6-l2（外部化超大 Handshake 存储）** 会解释 `all-memory` 这一段为什么能把 BRAM/存储从设计里剥离出去——它是降低本讲看到的那个 141 倍数字的关键手段之一，对应 Task 6 的资源最小化方向。
- **建议继续阅读的源码**：动手用 `python3 -c` 构造一个小 `modules` 字典喂给 [leaf_counts](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/write_utilization_report.py#L54-L78)，对照 `stat.json` 的输出格式，把「递归 → 归类 → 容量对比」整条链亲手跑一遍。
