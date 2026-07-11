# 外部化超大 Handshake 存储

## 1. 本讲目标

本讲承接 [u6-l1 自测外壳的自动生成](u6-l1-selftest-wrapper-autogen.md)，聚焦 TinyStories-1M 在 Yosys 综合阶段的另一个关键工程动作：**把超大的 Handshake 存储从设计里「外部化」出去**。

学完本讲，你应当能够：

1. 说清「为什么要 externalize」——即把巨型存储 blackbox 成板载 DDR 候选，让资源报告只度量控制/数据流「外壳」（shell）逻辑，而不是被装不下的存储淹没。
2. 读懂 `externalize_large_memories.py` 如何用两条正则 `MODULE_RE` / `MEMORY_RE` 逐行扫描 RTLIL 文本，累加出每个 `\handshake_memory_*` 模块的总位宽 `total_bits`。
3. 解释 128 kbit 阈值的含义、筛选规则与排序逻辑。
4. 看懂生成的 Yosys `blackbox` + `setattr` 脚本如何把选中模块变成带标记的空壳，以及 JSON 报告的字段。
5. 跟踪 `flake.nix` 里 `mkTinyStoriesSelftestBundle` 的三步顺序：`model-opt → externalize → shell`，并理解它如何与分阶段综合（u5-l4）、资源报告（u5-l3）串成 `all-memory` 流程。

## 2. 前置知识

本讲默认你已掌握以下概念（前序讲义已建立），这里只做最简提醒：

- **RTLIL**：Yosys 的文本中间表示（RTL Intermediate Language）。`.il` 文件用纯文本描述模块、连线、单元和存储。本讲的扫描器就是一个**逐行正则解析器**，不调用 Yosys 的 API，所以你必须先对 RTLIL 的文本格式有直觉。
- **`\handshake_memory_*` 模块**：CIRCT 把 Handshake 弹性数据流的内部存储降级到 HW 后，会生成名为 `\handshake_memory_N` 的模块（见 [u3-l2](u3-l2-cf-to-handshake-dataflow.md) 与 [u3-l3](u3-l3-handshake-to-hw-esi.md)）。它们是 LLM 权重/激活的落点，因此体积巨大。
- **blackbox**：Yosys 里把一个模块替换成「只有端口、没有内部实现」的占位符。综合时不再展开它，资源统计里它的内部叶单元计为零。
- **Handshake 方言是资源大户**：3e 报告已指出，即便 externalize 掉超大存储，整个 shell 设计仍约为目标 FPGA 的 141 倍 LUT（见 [u5-l3](u5-l3-utilization-report.md)）。externalize 是「必要但不充分」的一步。
- **`runCommand` / Nix 派生**：每个 shell/Python 步骤都被包成一个可缓存的派生（见 [u3-l5](u3-l5-pipeline-nix-orchestration.md)）。

一个 RTLIL 片段长这样（手写示意，仅为本讲说明格式，标注为示例代码）：

```text
module \handshake_memory_42
  memory width 32 size 4096 \weights
  memory width 32 size 4096 \biases
endmodule
```

- `module <名字>` 起一个模块（名字常以 `\` 转义开头）。
- `memory width <W> size <N> <名字>` 声明一个存储：字宽 `W` 比特、共 `N` 个字。该存储的总位数是 \(W \times N\)。
- `endmodule` 收尾（本讲的脚本其实**不依赖** `endmodule`，它靠「下一个 `module` 行」来切断当前模块，下文会讲为什么）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `scripts/pipeline/externalize_large_memories.py` | 主角：扫描 RTLIL，挑出总位宽 ≥ 阈值的 `\handshake_memory_*` 模块，产出 blackbox 脚本与 JSON 报告。 |
| `flake.nix` | 编排层：`mkExternalizedMemoryPlan` 调用上面脚本；`mkTinyStoriesSelftestBundle` 把 `model-opt → externalize → shell → 分阶段综合 → 资源报告` 串成 `all-memory` 目标。 |
| `scripts/pipeline/sv_to_il.sh` | 上游：产出本讲输入 RTLIL（`.il`）。它刻意用 `--no-proc` 跳过重活，这是本讲需要 `model-opt` 步骤的原因。 |
| `deliverables/3e-tiny-stories-1m-resource-report.md` | 工程结论：externalize 之后仍超配 141 倍，下一步是 Task 6 资源最小化。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：①RTLIL 模块/存储扫描，②128 kbit 阈值筛选，③blackbox 外部化与报告，④`flake.nix` 的 `all-memory` 编排。

### 4.1 RTLIL 模块/存储扫描

#### 4.1.1 概念说明

外部化的第一个问题是：**怎么知道设计里有哪些存储、每个有多大？**

最直接的办法是调用 Yosys 的 `select -list` 之类命令，但本项目选了另一种更轻、更可控的方式——**把 RTLIL 当纯文本，用两条正则逐行扫**。这样做有三个好处：

1. 不需要在扫描阶段跑 Yosys（Yosys 在 141 倍超配的设计上本身就吃内存）。
2. 扫描逻辑完全透明、可审计，就一个 Python 文件。
3. 扫描产物（blackbox 脚本 + JSON 报告）是纯文本，可缓存、可 diff。

核心思想是维护一个「当前模块」游标 `current`：遇到 `module` 行就切换游标，遇到 `memory` 行就把位宽累加进当前游标。

#### 4.1.2 核心流程

```
打开 .il 文件，逐行读取：
  若匹配 ^module <名字>：
      先把「上一个」current 若达标则收进 selected
      若名字以 \handshake_memory_ 开头 → 新建 current = {name, memories:[], total_bits:0}
      否则 → current = None（不关心非 Handshake 存储模块）
  否则若 current 非空 且 匹配 ^memory width W size N <名字>：
      bits = W * N（W 缺省为 1）
      把 {name,width,size,bits} 追加进 current.memories
      current.total_bits += bits
循环结束后：把最后一个 current 若达标也收进 selected
```

注意一个关键设计：**用「下一个 module 行」而不是 `endmodule` 来结束一个模块**。这意味着扫描器不需要知道 `endmodule`，只要 `module` 行能可靠出现即可。代价是循环结束后必须**手动补一次 flush**（最后一个模块后面没有下一个 `module` 行来触发它）。

#### 4.1.3 源码精读

两条正则定义在文件顶部：

[scripts/pipeline/externalize_large_memories.py:12-13](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/externalize_large_memories.py#L12-L13) —— `MODULE_RE` 匹配模块头并捕获名字；`MEMORY_RE` 匹配存储声明，其中 `width (\d+)` 是**可选**组（缺失时回退到 1），`size (\d+)` 是必填组，最后捕获存储名。

逐行扫描的主体在 `collect_selected_modules`：

[scripts/pipeline/externalize_large_memories.py:20-30](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/externalize_large_memories.py#L20-L30) —— 遇到 `module` 行时，**先**把上一个 `current`（若位宽达标）收进 `selected`，**再**根据名字是否以 `\handshake_memory_` 开头来决定新建游标还是置空。这一「先结算上一个、再开下一个」的顺序正是前文说的「靠下一个 module 行切断当前模块」。

[scripts/pipeline/externalize_large_memories.py:35-50](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/externalize_large_memories.py#L35-L50) —— 只有 `current` 非空时才解析 `memory` 行。第 39 行 `width = int(memory_match.group(1) or "1")` 处理 `width` 缺省为 1 的情形；第 41 行 `bits = width * size` 算出该存储位数；第 50 行累加进 `total_bits`。

[scripts/pipeline/externalize_large_memories.py:52-54](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/externalize_large_memories.py#L52-L54) —— 循环外的手动 flush：补结算最后一个模块，再按 `(total_bits, name)` **降序**排序（最大的排最前，便于报告阅读）。

#### 4.1.4 代码实践

**实践目标**：在不跑 Yosys 的前提下，验证两条正则确实能从 RTLIL 文本里提取出每个存储的位宽。

**操作步骤**：

1. 准备一个极小的 RTLIL 输入（示例代码，可另存为 `sample.il`）：

   ```text
   module \top
     memory width 8 size 16 \scratch
   endmodule
   module \handshake_memory_0
     memory width 32 size 4096 \w0
     memory width 32 size 4096 \w1
   endmodule
   module \handshake_memory_1
     memory size 1024 \flag
   endmodule
   ```

2. 直接调用扫描器（阈值设 0，列出全部 Handshake 存储模块）：

   ```bash
   python3 scripts/pipeline/externalize_large_memories.py \
     --input sample.il \
     --output-script out.ys \
     --output-report out.json \
     --min-module-bits 0
   cat out.json
   ```

**需要观察的现象**：`out.json` 的 `selected_modules` 里**只有** `\handshake_memory_0` 和 `\handshake_memory_1`，没有 `\top`；`\handshake_memory_0` 的 `total_bits` 应为 \(32 \times 4096 + 32 \times 4096 = 262144\)；`\handshake_memory_1` 的 `total_bits` 应为 \(1 \times 1024 = 1024\)（验证了 `width` 缺省为 1）。

**预期结果**：`selected_modules` 按 `total_bits` 降序，`\handshake_memory_0` 排在 `\handshake_memory_1` 之前。

> 待本地验证：若你机器上没装 Python 3.11+，可用任何能跑 `match`/`re` 的版本，本脚本只用了标准库。

#### 4.1.5 小练习与答案

**练习 1**：为什么扫描器忽略 `\top` 这种非 Handshake 模块里的存储？
**答案**：因为第 27 行的 `name.startswith(r"\handshake_memory_")` 把游标 `current` 直接置为 `None`；只有 Handshake 存储模块才被跟踪。其它模块（如顶层、控制器）即便有小的 scratch pad，也不属于「LLM 权重级」的大存储，外部化它们没意义。

**练习 2**：如果某个 `memory` 行写成 `memory size 1024 \x`（省略 `width`），`bits` 是多少？
**答案**：`width` 缺省为 1，故 `bits = 1 × 1024 = 1024`。这正是 `MEMORY_RE` 把 `width` 设成可选组的用意——RTLIL 里一比特宽的存储可以省略 `width`。

### 4.2 128 kbit 阈值筛选

#### 4.2.1 概念说明

扫出所有 Handshake 存储模块后，**不能全部外部化**——小存储本就可以用片上 BRAM 实现，外部化它们反而要付出「跨芯片访存延迟」的代价。于是需要一个阈值：**只有「大到一个片上 BRAM 块装不下、理应放板载 DDR」的存储才外部化**。

项目把这个阈值定在 **128 kbit**，即 \(\text{min\_module\_bits} = 128 \times 1024 = 131072\) 比特。作为参照，Xilinx 7 系单片 BRAM36 是 36 Kb ≈ 36864 比特，128 kbit 至少需要 4 片 BRAM36 才能装下；达到这个量级的存储按工程经验更适合当作片外存储。注意：脚本本身**不依赖**这个具体数字，它只是 `flake.nix` 传入的一个参数，因此换阈值无需改 Python 代码。

#### 4.2.2 核心流程

筛选条件是**模块粒度**而非单条存储粒度：

\[
\text{外部化} \iff \text{module.total\_bits} \ge \text{min\_module\_bits}
\]

也就是说，一个 `\handshake_memory_*` 模块里往往有**多条**存储（如权重矩阵被拆成多块），脚本把它们**求和**得到 `total_bits`，再用模块总和去比阈值。这样能捕获「单条不大、但模块合计很大」的情况。

筛选发生在两个时机（两处 `if` 完全对称）：

1. 遇到下一个 `module` 行时，结算上一个模块；
2. 文件读完时，结算最后一个模块。

#### 4.2.3 源码精读

阈值比较就两行，分别在「切换模块时」和「收尾时」：

[scripts/pipeline/externalize_large_memories.py:22-23](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/externalize_large_memories.py#L22-L23) —— 切换模块时结算上一个：`int(current["total_bits"]) >= min_module_bits` 才收进 `selected`。

[scripts/pipeline/externalize_large_memories.py:52-53](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/externalize_large_memories.py#L52-L53) —— 文件结尾的对称结算。这两处用的是 `>=`（meet or exceed），即正好等于 131072 也会被外部化。

阈值默认值不在 Python 里，而在 Nix 里：

[flake.nix:551-552](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L551-L552) —— `mkExternalizedMemoryPlan` 的参数 `minModuleBits ? (128 * 1024)`，默认 131072，调用方可覆盖。

#### 4.2.4 代码实践

**实践目标**：观察阈值如何改变被外部化的模块集合。

**操作步骤**：用 4.1.4 的 `sample.il`，分别跑两次：

```bash
# 阈值 200000：只有 \handshake_memory_0 (262144) 入选
python3 scripts/pipeline/externalize_large_memories.py \
  --input sample.il --output-script a.ys --output-report a.json --min-module-bits 200000
# 阈值 0：两个都入选
python3 scripts/pipeline/externalize_large_memories.py \
  --input sample.il --output-script b.ys --output-report b.json --min-module-bits 0
diff <(jq '.selected_module_count' a.json) <(jq '.selected_module_count' b.json)
```

**需要观察的现象**：`a.json` 的 `selected_module_count` 为 1，`b.json` 为 2。

**预期结果**：抬高阈值会减少 `blackbox` 脚本里的模块条目；`selected_total_bits` 字段随之变化。

#### 4.2.5 小练习与答案

**练习 1**：为什么筛选按「模块总位宽」而非「单条存储位宽」？
**答案**：一个 `\handshake_memory_*` 模块常含多条存储（权重被分块）。按模块求和能避免「每条都卡在阈值以下、合计却巨大」的漏网情况，让外部化决策落在「这个存储模块整体是否值得搬片外」这个更有工程意义的粒度上。

**练习 2**：若想让阈值变成 256 kbit，改哪里？
**答案**：不改 Python。在 `flake.nix` 调用 `mkExternalizedMemoryPlan` / `mkTinyStoriesSelftestBundle` 时传 `minModuleBits = 256 * 1024`（或 `externalMemoryMinModuleBits = 256 * 1024`）即可，因为该值会经 `toString` 注入 `--min-module-bits`。

### 4.3 blackbox 外部化与报告

#### 4.3.1 概念说明

挑出大存储模块后，要把它们从「会被综合展开的实现」变成「带端口的空壳」。Yosys 里这件事由 `blackbox` 命令完成。本讲生成的不是直接跑综合，而是**一段 Yosys 脚本** `externalize.ys`，由下游派生在综合前 `script externalize.ys` 加载。

每个被外部化的模块会得到四条命令：

- `select -clear` —— 清空当前选择集，确保操作目标干净。
- `select -module <名字>` —— 把当前选择集锁定到这一个模块。
- `setattr -mod -set llm2fpga_external_memory 1` —— 给该模块打一个属性标记 `llm2fpga_external_memory=1`，便于下游/审查时识别「这些是被 LLM2FPGA 外部化的存储」。
- `blackbox` —— 把当前选中模块替换成 blackbox（保留端口、抹掉内部实现）。

外部化的目的是让资源报告度量**外壳（shell）逻辑**——即围绕存储的控制流、数据流握手、算子实现——而不是度量那些反正装不下、理应放片外 DDR 的巨型存储。脚本注释把这一意图写得很清楚：«shell synthesis measures controller/fabric logic while treating these modules as external storage candidates»。

> 重要提醒（来自 3e 报告）：即便 externalize 掉所有超大存储，shell 设计**仍约 141 倍超配**。所以外部化是「让资源报告有意义」的必要步骤，但不是「让设计装得下」的充分方案。

#### 4.3.2 核心流程

```
对每个 selected 模块 m，向脚本追加：
    select -clear
    select -module m.name
    setattr -mod -set llm2fpga_external_memory 1
    blackbox
最后追加一条 select -clear 收尾
写出 externalize.ys
同时写出 report.json：
    input, min_module_bits,
    selected_module_count, selected_total_bits, selected_modules[]
```

JSON 报告里最有价值的字段是 `selected_total_bits`——它告诉你「如果把所有外部化存储放进片外，总共要多少比特带宽/容量」，这是后续评估 DDR 方案的关键输入。

#### 4.3.3 源码精读

脚本生成逻辑：

[scripts/pipeline/externalize_large_memories.py:77-93](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/externalize_large_memories.py#L77-L93) —— 先写四行注释头说明用途；随后对每个选中模块追加那四条命令；末尾 `script_lines.append("select -clear")` 收尾，最后用 `"\n".join(...)` 落盘。注意 `setattr` 在 `blackbox` **之前**执行——属性会随 blackbox 后的占位符一起保留，下游 `select -attr` 仍能筛出这些模块。

JSON 报告生成：

[scripts/pipeline/externalize_large_memories.py:95-102](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/externalize_large_memories.py#L95-L102) —— `report` 字典含 `selected_total_bits`（所有外部化模块位宽之和）与 `selected_modules`（每个模块的名字、各存储明细、`total_bits`），用 `json.dumps(..., indent=2, sort_keys=True)` 写成稳定排序的 JSON，便于 diff 与缓存命中。

`selected_total_bits` 的求和：

[scripts/pipeline/externalize_large_memories.py:75](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/externalize_large_memories.py#L75) —— `sum(int(module["total_bits"]) for module in selected)`。

#### 4.3.4 代码实践

**实践目标**：读懂生成的 blackbox 脚本结构，并与报告字段对账。

**操作步骤**：用 4.1.4 的命令产出 `out.ys` 与 `out.json`，然后：

```bash
cat out.ys
jq '.selected_total_bits, [.selected_modules[] | {name, total_bits}]' out.json
```

**需要观察的现象**：`out.ys` 里每个模块对应固定的「`select -clear` / `select -module` / `setattr ...` / `blackbox`」四行块，最后一条是单独的 `select -clear`；模块出现顺序与 `out.json` 中 `selected_modules` 的降序一致。

**预期结果**：在 4.1.4 的 `sample.il`、阈值 0 下，`out.ys` 里 `\handshake_memory_0` 块排在 `\handshake_memory_1` 块之前；`selected_total_bits` 等于 \(262144 + 1024 = 263168\)。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `setattr` 要在 `blackbox` 之前执行？
**答案**：`blackbox` 会把模块的内部实现抹掉，只留端口与属性。先 `setattr` 再 `blackbox`，标记 `llm2fpga_external_memory=1` 才会被保留到 blackbox 占位符上；若顺序反过来，`setattr` 作用在已空壳的模块上虽然技术上也能写属性，但语义上「先标记、再抹实现」更清晰，也避免在某些 Yosys 版本里 blackbox 后选择集变化导致的隐患。

**练习 2**：下游综合加载这个脚本后，被外部化模块在 `write_utilization_report.py`（u5-l3）的叶单元统计里会贡献多少？
**答案**：贡献为零。blackbox 模块没有内部叶单元，`leaf_counts` 递归到它时找不到子实例，所以 LUT/FF/DSP/BRAM 计数都是 0。这正是「让资源报告只度量 shell 逻辑」的实现机制。

### 4.4 `flake.nix` 的 `all-memory` 编排

#### 4.4.1 概念说明

外部化不是孤立的一步，它被嵌进一条更大的流水线：从降级链产出的 `.il` 出发，先清洗、再扫描外部化、再把 blackbox 应用回去得到「shell」RTLIL，最后走分阶段综合（u5-l4）出 mapped JSON，交给资源报告（u5-l3）。这条链全部由 `flake.nix` 用 `runCommand` 包成可缓存派生。

目标名 `tiny-stories-1m-baseline-float-selftest-all-memory-utilization` 里的 `all-memory` 段，指的就是「把所有 ≥128 kbit 的 Handshake 存储都外部化」这一配置（见 u1-l4 的命名约定）。它由 `mkTinyStoriesSelftestBundle` 实现，默认 `externalMemoryMinModuleBits = 128 * 1024`。

#### 4.4.2 核心流程

`mkTinyStoriesSelftestBundle` 的核心是三步派生顺序（即本讲实践任务要解释的对象）：

```
modelIl（来自降级链 sv_to_il.sh）
   │
   ▼  ① model-opt：proc + opt_expr + opt_clean + clean，产出规范 RTLIL
modelOptIl
   │
   ▼  ② externalize：扫描 modelOptIl，产出 blackbox 脚本 + JSON 报告
externalMemoryPlan (externalize.ys, report.json)
   │
   ▼  ③ shell：read modelOptIl → script externalize.ys → write_rtlil，得到「外壳」RTLIL
modelShellIl
   │
   ▼  分阶段综合（mkSynthJsonStages，u5-l4）+ 顶层 selftest SV
stages.json
   │
   ▼  write_utilization_report.py（u5-l3）对比 fpgaCapacities
utilizationReport (summary.txt / summary.json / stat.json)
```

三步顺序的关键约束：**必须先 `opt` 再 `externalize`，最后 `shell`**。下文解释为什么。

#### 4.4.3 源码精读

`mkExternalizedMemoryPlan` 把 Python 脚本包成一个派生：

[flake.nix:551-564](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L551-L564) —— 用 `python3` 调 `externalize_large_memories.py`，传四个参数；把产出的 `externalize.ys` 与 `report.json` 复制进 `$out`。注意它**只产脚本，不跑综合**——这是「生成器」与「消费者」分离的设计。

三步顺序在 `mkTinyStoriesSelftestBundle` 里：

[flake.nix:570-581](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L570-L581) —— **① model-opt**：`mkYosysRtlil` 读原始 `modelIl`，跑 `proc`（把 `--no-proc` 推迟的 `always`/过程块转成数据流）、`opt_expr`/`opt_clean`/`clean`（去死连线和冗余单元），产出规范、稳定的 `modelOptIl`。这一步是必要的，因为 `sv_to_il.sh`（见 [scripts/pipeline/sv_to_il.sh:25-29](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/sv_to_il.sh#L25-L29)）刻意只做 `hierarchy/stat/write_rtlil`，把 `proc` 这类重活推给下游。

[flake.nix:582-586](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L582-L586) —— **② externalize**：`mkExternalizedMemoryPlan` 以 `modelOptIl` 为输入，产出 blackbox 脚本与报告。**必须在 opt 之后**：opt 之前的 RTLIL 因 `--no-proc` 尚不规范，`memory` 声明与模块边界未稳定，逐行扫描不可靠。

[flake.nix:587-595](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L587-L595) —— **③ shell**：再次 `mkYosysRtlil`，读**同一个** `modelOptIl`（不是原始 `modelIl`），执行 `script ${externalMemoryPlan}/externalize.ys` 应用 blackbox，再 `hierarchy -top main -check`、`write_rtlil`，得到 `modelShellIl`——即「大存储已被掏空」的外壳 RTLIL。这一步**不重新 opt**，因为 `externalize.ys` 只做 `select/setattr/blackbox`，不改其它逻辑，复用 opt 后的规范形态即可。

[flake.nix:596-605](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L596-L605) —— 把 `modelShellIl` 与手写的顶层 selftest SV（见 u6-l1）一起喂给 `mkSynthJsonStages`（u5-l4 的 9 阶段综合），出 mapped JSON，再经 `mkMappedJsonUtilizationReport`（u5-l3）与 `fpgaCapacities`（[flake.nix:249-256](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L249-L256)，XC7K480T）对比，产出 `summary.txt/json` 与 `stat.json`。

最终绑定到 package：

[flake.nix:672-678](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L672-L678) —— `tinyStories1mBaselineFloatSelftestAllMemory` 调用 bundle；[flake.nix:796-798](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L796-L798) 把它的 `.utilizationReport` 暴露为 gate 目标。

#### 4.4.4 代码实践

**实践目标**：解释 `model-opt → externalize → shell` 三步顺序的因果关系（本讲规格指定的任务）。

**操作步骤**：

1. 读 [flake.nix:566-606](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L566-L606)，在纸上画出三步的派生依赖箭头，标注每一步的输入来源与产出。
2. 回答两个问题：
   - 为什么 `externalize` 必须消费 `modelOptIl` 而不是原始 `modelIl`？
   - 为什么 `shell` 步骤读的是 `modelOptIl` + 外部化脚本，而不是再跑一遍 `proc/opt`？
3. （可选）跑 gate 命令查看真实报告：

   ```bash
   nix build .#tiny-stories-1m-baseline-float-selftest-all-memory-utilization -L
   cat result/summary.txt
   ```

**需要观察的现象**：`summary.txt` 的 `clb_luts` 仍远超 `fpgaCapacities.clb_luts = 298600`（约 141 倍），证明「externalize 让报告只度量 shell」与「shell 本身仍超配」是两件事。

**预期结果**：能复述——`opt` 把 `--no-proc` 推迟的工作补齐、让 RTLIL 规范；`externalize` 基于规范 RTLIL 生成 blackbox 脚本（只读不改）；`shell` 把脚本应用回**同一份**规范 RTLIL，得到大存储被掏空的外壳，交给综合。三步各自是独立 Nix 派生，改阈值只重算 `externalize` 及其下游（`shell`、综合、报告），不重算 `model-opt`。

> 待本地验证：gate 命令需要先构建整套工具链（torch-MLIR/CIRCT/Yosys 等），首次运行耗时极长且需要较大内存。

#### 4.4.5 小练习与答案

**练习 1**：如果只想外部化 256 kbit 以上的存储，要改哪些派生？哪些派生会重算、哪些不会？
**答案**：在 `mkTinyStoriesSelftestBundle` 调用处传 `externalMemoryMinModuleBits = 256 * 1024`。由于阈值是 `mkExternalizedMemoryPlan` 的输入参数，`externalMemoryPlan` 派生的指纹变化 → 它本身重算 → 下游 `modelShellIl`、`stages`、`utilizationReport` 重算；上游 `modelOptIl` 的指纹不变，**不**重算（这正是 u3-l5 讲过的惰性缓存）。

**练习 2**：目标名里的 `all-memory` 与脚本里 `setattr -set llm2fpga_external_memory 1` 的属性名有何关系？
**答案**：`all-memory` 是 Nix 目标命名约定（见 u1-l4），表示「外部化所有 ≥128 kbit 的 Handshake 存储」这一配置；`llm2fpga_external_memory` 是写回 RTLIL 的 Yosys 属性标记，供下游工具/审查识别单个被外部化的模块。前者是 build target 语义，后者是网表属性，二者层次不同但指向同一组模块。

## 5. 综合实践

把本讲四个模块串起来：**用一份小 RTLIL 走完「扫描 → 阈值筛选 → blackbox 生成 → 应用 → 资源对比」全流程，验证外部化如何改变叶单元统计。**

1. 自造一份含一个大 Handshake 存储模块、一个小 Handshake 存储模块、一个非 Handshake 模块的 RTLIL（参考 4.1.4 的 `sample.il`，把大模块的 `total_bits` 设到 ≥ 131072，小的设到几百比特）。
2. 用 `externalize_large_memories.py --min-module-bits 131072` 生成 `externalize.ys` 与 `report.json`。
3. 手工检查 `externalize.ys`：应当只 blackbox 那个大模块，小模块与非 Handshake 模块不出现。
4. 写一段 Yosys 脚本：`read_rtlil sample.il` → `hierarchy -top \top` → `stat`（记录外部化前的模块）；再 `script externalize.ys` → `stat`（记录外部化后），对比两次 `stat` 输出中大模块的差异（blackbox 后该模块的内部单元消失）。
5. 回答：如果把阈值降到 0，`report.json` 的 `selected_total_bits` 会变成多少？这对应「把所有 Handshake 存储都搬到片外」的总带宽/容量需求。

> 待本地验证：第 4 步需要安装 Yosys；若没有，可只在 `stat` 文本层面推理（blackbox 模块在 `stat` 里会显示为 `blackbox`，无内部 cell 计数）。

## 6. 本讲小结

- **外部化的本质**：把超大 Handshake 存储模块 blackbox 成带 `llm2fpga_external_memory=1` 标记的空壳，让资源报告只度量控制/数据流「shell」逻辑，把巨型存储当作板载 DDR 候选。
- **扫描器是纯文本正则**：`MODULE_RE` 切模块、`MEMORY_RE` 收存储位宽，靠「下一个 module 行」切断当前模块，故循环外必须手动 flush 最后一个模块。
- **只跟踪 `\handshake_memory_*`**：非 Handshake 模块的游标置 `None`，小存储不值得外部化。
- **阈值是模块粒度**：`total_bits = Σ(width × size)`，与 `min_module_bits = 128×1024 = 131072` 比较（`>=`），阈值由 Nix 注入、改阈值无需动 Python。
- **产物是脚本而非综合**：生成 `externalize.ys`（`select/setattr/blackbox` 四行块）+ `report.json`（含 `selected_total_bits`），由下游派生消费。
- **三步顺序**：`model-opt`（补 `proc`、规范 RTLIL）→ `externalize`（扫描生成脚本）→ `shell`（把脚本应用回同一份规范 RTLIL 得到外壳），再接 u5-l4 分阶段综合与 u5-l3 资源报告；改阈值只重算 `externalize` 及下游。
- **必要不充分**：3e 报告证明即便 externalize 掉所有超大存储，shell 仍约 141 倍超配，故下一步是 Task 6 资源最小化（如更直接用板载内存、换掉 Handshake 方言）。

## 7. 下一步学习建议

- 阅读 [u6-l3 浮点原语的定点近似实现](u6-l3-fp-primitives-approximation.md)，看另一类「外部化」：浮点算子被 CIRCT 补丁降为 extern 后，如何用 Q16.16 定点 SV 提供可综合实现。
- 阅读 [u6-l4 CIRCT 补丁栈与瓶颈结论](u6-l4-circt-patches-and-bottleneck.md)，把 externalize（在 Yosys 层打补丁）与 patches/circt-task3-rfp（在 CIRCT 层打补丁）对照，理解「上游工具不足时」的两种工程应对。
- 想动手扩展：参照 `mkTinyStoriesSelftestBundle`，写一个调高 `externalMemoryMinModuleBits` 的派生，对比 `summary.txt` 的 `clb_luts` 变化，体会「外部化更多/更少存储」对 shell 资源报告的敏感度（预期变化不大，因主要负担在 Handshake 控制逻辑而非存储本身）。
