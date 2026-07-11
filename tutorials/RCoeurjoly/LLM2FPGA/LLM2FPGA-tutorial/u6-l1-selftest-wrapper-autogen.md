# 自测外壳的自动生成

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚**为什么**要在生成的 `main.sv` 外面包一层 `tiny_stories_selftest_top`，以及为什么这层外壳适合「自动生成」而不是手写。
- 看懂 `gen_tiny_stories_selftest_top.py` 如何用正则解析 `main.sv` 的端口表，把每一个端口还原成「方向 + 声明 + 位宽」三元组。
- 理解脚本对 **ESI 通道**的启发式识别：`choose_start_channel`、`choose_done_channel`、`choose_store_prefix` 与 load 通道扫描各自用什么命名规律挑出 start / done / store / load 四类通道。
- 掌握 load（带地址请求-数据响应）与 store（接收载荷后回一个 done token）两类握手是怎么用 `always_ff` + `assign` 生成出来的，以及 pass/fail 监控如何用 LED 反映结果。
- 能把手写版 `rtl/tiny_stories_selftest_top.sv` 与脚本生成的输出逐段对照，确认二者结构等价。

## 2. 前置知识

在进入本讲前，你需要先建立以下几个概念（相关讲义已覆盖的，这里只做一句话回扣）：

- **降级链的终点是 `main.sv`**：u3-l4 讲过，CIRCT 把 HW/seq 方言经 `--export-split-verilog` 导出成一批 `.sv` 文件，其中顶层模块固定叫 `main`，并附带 `sources.f` 文件清单。`main` 的端口就是整条降级链最终暴露出来的硬件接口。
- **ESI 弹性数据流通道**：u3-l2 / u3-l3 讲过，Handshake→HW 经 ESI 方言把抽象通道「溶解」成具体的 `data` / `valid` / `ready` 三根线。落到 `main.sv` 上，一个 ESI 通道并不是一个端口，而是**一组按命名约定分散的端口**，例如输入 token 通道 `in18` 由 `in18_valid`（输入）和 `in18_ready`（输出）两根线共同组成。
- **板级自测（selftest）思路**：u5-l2 讲过 matmul 的自测外壳——上电后自己播种输入、触发计算、用 LED 报 pass/fail，无需 PC 介入。本讲讲的是 TinyStories-1M 这条路线上的同类外壳。
- **「唯一真相源」是 PyTorch**：u4-l1 / u4-l2 讲过等价性验证。本讲的外壳**不再比对数值**（141 倍超配的设计没法真的跑完推理），而是用「在超时周期内 done 通道是否拉起」作为 pass 判据——这是工程上的降级验收。

> 一个关键事实（务必记住）：`main.sv` 是**构建产物**，不在 git 里（本仓库 `git ls-files` 查不到 `main.sv`）。它的端口表会随模型、随降级补丁、随外部化策略而变。这正是「外壳要自动生成」的根本原因——端口一变，手写的外壳就会和顶层对不上。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们一「生成器」一「产物」，是镜像关系：

| 文件 | 角色 | 关键内容 |
| --- | --- | --- |
| [scripts/pipeline/gen_tiny_stories_selftest_top.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py) | **生成器**（Python） | 解析 `main.sv` 端口、识别 ESI 通道、emit 一份完整的 `tiny_stories_selftest_top.sv` |
| [rtl/tiny_stories_selftest_top.sv](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/tiny_stories_selftest_top.sv) | **手写版外壳**（SystemVerilog） | 与生成器输出结构等价的、当前真正被构建消费的版本 |

构建侧的接线点在 `flake.nix` 的 `mkTinyStoriesSelftestBundle`：它把 `top = tinyStoriesSelftestTop`（即手写的 `rtl/tiny_stories_selftest_top.sv`）连同外部化后的模型 RTLIL 喂进 `mkSynthJsonStages` 做综合，最终只产出资源利用报告（141 倍超配，到不了比特流，回顾 u5-l2 / u5-l3）。

> 诚实地说明工程现状：目前 `flake.nix` **直接使用手写版** `rtl/tiny_stories_selftest_top.sv`，`gen_tiny_stories_selftest_top.py` **尚未被 `.nix` 文件调用**（全仓库 grep 无 nix 引用）。这个脚本的提交信息写的是 *"Tiny stories top module generated with python. Unideal"*——也就是说，它是「把手写外壳的生成过程自动化」这条方向上的产物，目的是当 `main.sv` 端口表变化时能重新派生出对应外壳，而不再靠人手维护。因此本讲的正确打开方式是：**把手写 `.sv` 当作「生成器应当产出什么」的参照答案**，两边逐段对照来理解生成逻辑。

## 4. 核心概念与源码讲解

### 4.1 解析 main.sv 端口

#### 4.1.1 概念说明

生成外壳的第一步，是回答一个看似简单的问题：**「顶层 `main` 到底有哪些端口？」**

这件事之所以不简单，是因为 SystemVerilog 的端口表写法很自由：

- 方向关键字（`input`/`output`）可以只在**一组连续端口的最开头写一次**，后面的端口沿用前一个方向（ANSI 风格的「方向继承」）。
- 端口声明里可能带位宽（`logic [63:0]`）、可能带 packed struct（`struct packed {logic [15:0] address; logic [31:0] data;}`）、也可能是单比特无显式声明。
- 端口之间用逗号分隔，但换行、注释、多余空格都会出现。

脚本不能用一个「成熟 SV 解析器」（那会引入重依赖），而是用**正则 + 状态机**做一次「够用就好」的轻量解析，把每个端口还原成下面这个不可变三元组：

```python
@dataclass(frozen=True)
class Port:
    name: str        # 端口名，如 in16_ld0_data
    direction: str   # "input" 或 "output"
    decl: str        # 去掉名字后的声明，如 "logic [63:0]" 或 "struct packed {...}"
    bits: int | None # 位宽；struct 返回 None（无法静态确定）
```

参见 [scripts/pipeline/gen_tiny_stories_selftest_top.py:18-L23](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L18-L23)。

#### 4.1.2 核心流程

`parse_main_ports` 的执行过程可以用下面这段伪代码概括：

```
读取 main.sv 全文
  ↓
正则定位 module main ( ... ); 的括号内部分（含换行，跨行匹配）
  ↓
剥掉行内注释 //...
  ↓
按逗号切 token，每个 token 压成单行单空格
  ↓
逐 token 维护「当前方向 current_direction」「当前声明 current_decl」：
    - 若 token 以 input/output 开头 → 更新 current_direction，记录声明
    - 否则 → 沿用 current_direction（实现「方向继承」）
    - 用正则从 token 末尾抠出端口名，剩下的当作 decl
  ↓
对每个端口调 parse_decl_bits 算位宽
  ↓
返回 list[Port]
```

位宽计算 `parse_decl_bits` 的规则值得单独记住（见 [scripts/pipeline/gen_tiny_stories_selftest_top.py:31-L43](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L31-L43)）：

- 空声明 → 1 比特；
- 含 `struct`/`union` → 返回 `None`（位宽未知，后续需要的地方会因此报错熔断）；
- 否则找出所有 `[msb:lsb]` 区间，把每个区间的宽度 `abs(msb-lsb)+1` **相乘**（多维数组情形），得到总位宽。

数学上，对一个声明里出现的所有位区间 \([m_i:l_i]\)，总位宽为：

\[
\text{bits} = \prod_i \big(|m_i - l_i| + 1\big)
\]

#### 4.1.3 源码精读

模块头定位与注释剥离——注意 `flags=re.S` 让 `.` 匹配换行，从而跨行抓到整个端口表（[scripts/pipeline/gen_tiny_stories_selftest_top.py:50-L56](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L50-L56)）：

```python
text = main_sv.read_text(encoding="utf-8")
m = re.search(r"\bmodule\s+main\s*\((.*?)\);\s", text, flags=re.S)
if m is None:
    die(f"unable to find module header in {main_sv}")
header = strip_line_comments(m.group(1))
raw_tokens = [t.strip() for t in header.replace("\n", " ").split(",") if t.strip()]
```

「方向继承」的核心循环——当一个 token 不带方向关键字时，沿用上一轮的 `current_direction`（[scripts/pipeline/gen_tiny_stories_selftest_top.py:62-L94](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L62-L94)）：

```python
for token in raw_tokens:
    md = re.match(r"^(input|output)\b(.*)$", rest)
    if md is not None:
        direction = md.group(1)            # 本 token 显式声明了方向
        rest = md.group(2).strip()
    mn = re.search(r"([A-Za-z_][A-Za-z0-9_$]*)\s*$", rest)  # 名字一定在末尾
    name = mn.group(1)
    decl = rest[: mn.start(1)].strip()     # 名字左边的就是声明
    if direction is not None:
        current_direction = direction
        current_decl = decl
    else:
        if decl:
            current_decl = decl            # 沿用方向，可能更新声明
```

这段代码之所以「抠名字从末尾抠」，是因为 SV 端口声明永远是「方向 + 类型 + 名字」顺序，名字固定在最右侧；用 `([A-Za-z_][A-Za-z0-9_$]*)\s*$` 锚定行尾就能稳稳抓到，剩下的前缀就是声明。

#### 4.1.4 代码实践

**实践目标**：验证解析器对「方向继承」和 packed struct 的处理是否符合预期。

**操作步骤**：

1. 在临时目录下写一个最小 `main.sv`（**示例代码**，不是仓库原文件）：

   ```systemverilog
   module main(
     input  logic        clock,
     input  logic        reset,
     input  logic        in0_valid,
     output logic        in0_ready,
     output logic [63:0] in0_ld0_addr,
     output logic        in0_ld0_addr_valid,
     input  logic        in0_ld0_addr_ready,
     input  logic [63:0] in0_ld0_data,
     input  logic        in0_ld0_data_valid,
     output logic        in0_ld0_data_ready,
     struct packed {logic [15:0] address; logic [31:0] data; } in1_st0,
     output logic        out0_valid,
     input  logic        out0_ready
   );
     // ... 省略实现
   endmodule
   ```

2. 进入 `nix develop`（回顾 u1-l3，得到带 Python 的环境）。
3. 跑一段一次性脚本（**示例代码**）调 `parse_main_ports`，打印每个 `Port`：

   ```python
   from pathlib import Path
   import sys; sys.path.insert(0, "scripts/pipeline")
   from gen_tiny_stories_selftest_top import parse_main_ports
   for p in parse_main_ports(Path("main.sv")):
       print(p.direction, repr(p.decl), p.bits, p.name)
   ```

**需要观察的现象**：

- `in0_ld0_addr_ready`、`in0_ld0_data`、`in0_ld0_data_valid` 都应是 `input`——即便它们前面没有再写 `input`（验证「方向继承」）。
- `in1_st0` 的 `bits` 应为 `None`（struct 触发）。
- `in0_ld0_data` 的 `bits` 应为 `64`。

**预期结果**：方向与位宽与上表一致。若你看到的 `in0_ld0_data_ready` 方向变成了 `output`，说明解析器把它和上一行 `in0_ld0_data_valid` 的方向弄反了——回去检查方向继承分支。

> 本地若无 `nix develop`，可手写等价的 Python 单测断言（`assert p.direction == "input"`）替代；脚本运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `parse_decl_bits` 遇到 `struct`/`union` 直接返回 `None`，而不是去解析结构体字段累加位宽？

**答案**：脚本刻意把「确定 struct 位宽」这件事推出去——`None` 在下游（`data_expr` / load 数据位宽推导）会触发 `die(...)` 熔断，提示维护者手动确认。这是一种 **fail-fast** 策略：与其算错一个看似合理的位数，不如停下来报错。回顾 u4-l1 的「快速失败」约定。

**练习 2**：端口表里第一行是 `input logic clock`，第二行是 `input logic reset`。如果有人在 `clock` 那行末尾漏写了逗号，解析器会怎样？

**答案**：`split(",")` 会把两行合并成一个 token，末尾名字正则会抓到 `reset`，声明里会残留 `clock` 的内容，得到一个名字对、声明错的 `Port`。这是轻量正则解析器的固有脆弱性，也正是脚本要配套手写版 `.sv` 做交叉验证的原因。

---

### 4.2 ESI 通道启发式识别

#### 4.2.1 概念说明

解析出端口表后，第二个问题是：**这些零散的端口，哪几个属于同一个 ESI 通道？每个通道又扮演什么角色？**

ESI 把一个抽象通道溶解成 `valid`/`ready`（可能还有 `data`）几根线，并按**命名约定**落到 `main.sv` 上。脚本不依赖任何 CIRCT 元数据，纯靠命名规律把端口重新「拼」回通道，并按职责分成四类：

| 角色 | 命名约定 | 在外壳里的职责 |
| --- | --- | --- |
| **start**（启动 token） | 输入 `in<N>_valid` + 输出 `in<N>_ready` | 上电后发一个零宽 token，把 DUT 启动一次 |
| **done**（完成 token） | 输出 `out<N>_valid` + 输入 `out<N>_ready` | DUT 算完会拉起它的 valid，作为 pass 判据 |
| **store**（DUT 向外写） | `in<N>_st0` / `_st0_valid` / `_st0_ready` / `_st0_done_valid` / `_st0_done_ready` | 接收载荷后，回一个 done token 完成握手 |
| **load**（DUT 向外读） | `in<N>_ld0_addr[_valid/_ready]` + `in<N>_ld0_data[_valid/_ready]` | DUT 给地址请求，外壳回固定数据 |

注意 store / load 都挂在 `in<N>` 前缀上：它们对 DUT 而言是「输入侧」（DUT 发起请求），所以编号在 `in` 序列里，这与 u3-l3 讲的「外部存储的 load/store 节点」一脉相承。

#### 4.2.2 核心流程

四类通道的选择策略可以统一概括为「**用命名正则筛候选 → 用配对约束（valid 的 ready 在对侧）过滤 → 按编号排序取端**」：

```
choose_start_channel:
  候选 = { in<N>_valid (输入) | 同名 _ready 在输出 }
  取候选里 N 最大的那个                  ← 启动 token 一般是「最后一个」输入通道

choose_done_channel:
  候选 = { out<N>_valid (输出) | 同名 _ready 在输入 }
  取候选里 N 最小的那个                  ← 完成 token 一般是「第一个」输出通道

choose_store_prefix:
  候选 = { in<N> | 存在 in<N>_st0[_done][_valid/_ready] 形态端口 }
  取候选里 N 最小的那个

load 通道扫描:
  对每个 in<N>，若同时存在
    in<N>_ld0_data (输入) ∧ in<N>_ld0_data_valid (输入)
    ∧ in<N>_ld0_addr_ready (输入) ∧ in<N>_ld0_addr_valid (输出)
  则认定 in<N> 是一个 load 通道；按 N 升序排列
```

「取最大 / 取最小」的排序规则统一由 `port_sort_key` 实现：它把 `in<N>` 映射成 `(N, name)`，非 `in<N>` 形态的排到 `(10**9, name)` 兜底（[scripts/pipeline/gen_tiny_stories_selftest_top.py:99-L103](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L99-L103)）。

#### 4.2.3 源码精读

`choose_start_channel`——筛输入侧 `in<N>_valid`、要求配对的 `_ready` 在输出侧、取编号最大者（[scripts/pipeline/gen_tiny_stories_selftest_top.py:122-L136](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L122-L136)）：

```python
for name in inputs:
    if re.fullmatch(r"in\d+_valid", name) is None:
        continue
    base = name[: -len("_valid")]
    ready = f"{base}_ready"
    if ready in outputs:
        idx = int(base[2:])
        candidates.append((idx, name, ready))
if not candidates:
    die("unable to detect start valid/ready channel")
candidates.sort()
_, valid, ready = candidates[-1]          # ← 取最大 idx
return valid, ready
```

`choose_done_channel` 是它的对偶——在输出侧筛 `out<N>_valid`、配对 `_ready` 在输入侧、取编号最小者（[scripts/pipeline/gen_tiny_stories_selftest_top.py:139-L153](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L139-L153)）：关键差异只有两处——遍历的是 `outputs`、最后取 `candidates[0]`（最小）。

> **为什么 start 取最大、done 取最小？** 这是一个**经验性的位置约定**，编码了 CIRCT Handshake→HW lowering 给 TinyStories-1M 排端口时的规律：控制用的「启动 token」往往排在一个函数输入参数表的**最后**（编号最大的 `in<N>`），而「完成 token」往往排在输出参数表的**最前**（编号最小的 `out<N>`）。脚本把这个约定硬编码成「取端值」的启发式。它的好处是：端口表一变，脚本能重新挑出通道，而**不需要**在 `flake.nix` 里写死 `in18` / `out0`。代价是：这是绑定在「本模型当前 lowering 顺序」上的经验规则，换个模型或换条降级路径就可能失灵——这正是脚本存在（可重新派生）而非硬编码的理由。

store 前缀选择——用一个可选项很多的正则一次性囊括 `_st0` / `_st0_done` / `_st0_valid` / `_st0_ready`，再取最小编号（[scripts/pipeline/gen_tiny_stories_selftest_top.py:156-L164](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L156-L164)）：

```python
prefixes = {
    m.group(1)
    for p in ports
    if (m := re.fullmatch(r"(in\d+)_st0(?:_done)?(?:_valid|_ready)?", p.name)) is not None
}
return sorted(prefixes, key=port_sort_key)[0]
```

load 通道识别靠一个模块级常量正则把同前缀的 6 根线归到一组（[scripts/pipeline/gen_tiny_stories_selftest_top.py:13-L15](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L13-L15)）：

```python
LOAD_PORT_RE = re.compile(
    r"^(in\d+)_ld0_(addr|addr_valid|addr_ready|data|data_valid|data_ready)$"
)
```

随后在 `generate_wrapper` 里按「方向配对」过滤出真正的 load 通道（[scripts/pipeline/gen_tiny_stories_selftest_top.py:186-L202](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L186-L202)）：必须 `data`/`data_valid`/`addr_ready` 都是输入、`addr_valid` 是输出，四者缺一不可，否则不当 load 处理。

load 数据的取值由 `data_expr` 决定（[scripts/pipeline/gen_tiny_stories_selftest_top.py:106-L119](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L106-L119)）：无地址时给一个固定常数（64 位给 `64'h0123_4567_89AB_CDEF`，32 位给 `32'h1357_9BDF`）；有地址时把地址寄存器 `<prefix>_addr_q` 按位宽补齐或截断回送。

#### 4.2.4 代码实践

**实践目标**：把启发式选择规则落到本仓库手写版 `.sv` 的真实通道编号上，验证「脚本挑出来的」与「手写版实际用的」一致。

**操作步骤**：

1. 打开 [rtl/tiny_stories_selftest_top.sv:17-L33](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/tiny_stories_selftest_top.sv#L17-L33)，这是手写版列出的 DUT 输入/输出端口。
2. 自己人工跑一遍四个 `choose_*`：
   - `in18_valid`(输入) + `in18_ready`(输出) → start 候选；`in16_ld0_*` 不匹配 `in\d+_valid`。start 取最大 → **in18**。
   - `out0_valid`(输出) + `out0_ready`(输入) → done 候选；done 取最小 → **out0**。
   - 含 `in17_st0`/`_st0_valid`/`_st0_ready`/`_st0_done_*` → store 前缀，取最小 → **in17**。
   - `in16_ld0_data`(输入) + `_data_valid`(输入) + `_addr_ready`(输入) + `_addr_valid`(输出) 齐全 → load 前缀 **in16**。
3. 对照手写版的注释行（第 48、59、78、81 行分别注释了 *start token* / *load* / *out0_ready* / *store*），确认四类通道编号一致。

**需要观察的现象**：四个 `choose_*` 选出的编号是 `in18 / out0 / in17 / in16`，与手写版驱动的端口完全吻合。

**预期结果**：完全一致。这正是「生成器输出 ≈ 手写版」的第一道、也是最关键的吻合点——通道选错，后面所有驱动逻辑都会接错线。

> 运行脚本本身需要一份真实 `main.sv`（构建产物），本仓库 git 里没有；若想端到端验证，可先 `nix build .#tiny-stories-1m-baseline-float-selftest-all-memory-utilization` 拿到产物里的 `main.sv`，再 `python scripts/pipeline/gen_tiny_stories_selftest_top.py --main-sv <path>/main.sv --out /tmp/gen_top.sv`，把 `/tmp/gen_top.sv` 与手写版 diff。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`choose_start_channel` 为什么要额外要求 `ready in outputs`，而不是只要看到 `in<N>_valid` 就算候选？

**答案**：单方向的 `valid` 可能来自一个**无反压**的单向输出指示，并不是一个完整的 ESI token 通道。真正的 token 通道必须 valid/ready **成对**且分居输入/输出两侧（valid 在发送方、ready 在接收方）。要求配对正是为了排除「看起来像但不是」的端口，避免把数据线误当成启动信号。

**练习 2**：如果某次 lowering 让完成 token 排到了 `out3` 而不是 `out0`，`choose_done_channel` 还能正确选中吗？

**答案**：能——只要 `out3_valid`/`out3_ready` 配对存在，它就是候选之一；脚本取的是「编号最小的**候选**」，不是字面意义上的 `out0`。但若 `out0_valid`/`out0_ready` 也存在且不是完成 token，启发式就会**误选**。这就是「取最小」启发式的固有风险，需要换模型时人工复核。

---

### 4.3 生成握手指令与 pass/fail 监控

#### 4.3.1 概念说明

挑出四类通道后，`generate_wrapper`（[scripts/pipeline/gen_tiny_stories_selftest_top.py:167-L405](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L167-L405)）要把它们「接上线」：决定哪些输入由外壳驱动、怎么驱动，以及怎么判定 pass/fail。

外壳的顶层接口固定只有三根对外引脚——`SYS_CLK`、`SYS_RSTN`、`led_3bits_tri_o[2:0]`（参见手写版 [rtl/tiny_stories_selftest_top.sv:1-L5](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/tiny_stories_selftest_top.sv#L1-L5)）。`main` 的几十根 ESI 端口全部是**内部**线网，由外壳在内部驱动或观察。LED 三位的含义固定：

| LED 位 | 信号 | 含义 |
| --- | --- | --- |
| `[0]` | `blink_count[25]` | 心跳：常闪表示外壳活着、时钟在跑 |
| `[1]` | `pass_latched` | 完成通道在超时前拉起 → pass |
| `[2]` | `fail_latched` | 超时未完成 → fail |

注意这是**降级版**的 pass 判据：只看 done 通道有没有在 `TIMEOUT_CYCLES`（5 千万周期）内拉起，**不**比对推理数值——因为 141 倍超配的设计（回顾 u1-l4 / u5-l3）本就不指望算出正确结果，只验证「降级链产出的硬件能跑完一次握手而不死锁」。

#### 4.3.2 核心流程

生成的 wrapper 由若干个相对独立的块拼成，时序上分四阶段：

```
① 上电 boot 复位
   boot_count 从 0 数到 BOOT_RESET_CYCLES(16) → 期间 reset=1，给 DUT 一个稳定复位窗口
   ② 发 start token
   在 start_valid 上拉一拍 1，等 start_ready 握手后撤销（只触发一次计算）
   ③a 响应 load（每个 load 通道各一份）
   DUT 给 addr_valid → 外壳给 addr_ready 并锁存请求 → 外壳在 data/data_valid 上回固定数据
   ③b 接 store
   DUT 给 st0_valid+载荷 → 外壳给 st0_ready 接住 → 外壳在 st0_done_valid 上回一个 done token
   ④ pass/fail 监控
   计数 cycle_count；若 done_valid 拉起 → pass_latched；若超时 → fail_latched；二者一旦置位即锁存
```

这里有两个关键握手状态机，状态都用 `*_pending` 寄存器表达：

- **load**：`{prefix}_pending` 为 0 表示「等地址请求」，为 1 表示「已收请求、待给数据」。
- **store**：`store_done_pending` 为 0 表示「等载荷」，为 1 表示「已收载荷、待发 done token」。

#### 4.3.3 源码精读

**start token**（生成侧 [scripts/pipeline/gen_tiny_stories_selftest_top.py:259-L267](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L259-L267)）与手写版（[rtl/tiny_stories_selftest_top.sv:49-L57](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/tiny_stories_selftest_top.sv#L49-L57)）逐行等价：

```systemverilog
// 手写版（in18 即脚本选出的 start 通道）
always_ff @(posedge SYS_CLK or negedge SYS_RSTN) begin
  if (!SYS_RSTN)        in18_valid <= 1'b1;
  else if (reset)       in18_valid <= 1'b1;
  else if (in18_valid && in18_ready) in18_valid <= 1'b0;  // 握手后撤销，只发一次
end
```

生成侧只是把 `in18` 换成变量 `{start_valid}` / `{start_ready}`，逻辑完全相同。

**load 响应**——这是「带地址请求-数据响应」的握手，状态机由 `load_pending` 驱动。生成侧（[scripts/pipeline/gen_tiny_stories_selftest_top.py:284-L302](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L284-L302)）：

```python
lines.append(f"      if (!{prefix}_pending && {addr_valid} && {addr_ready}) begin")
lines.append(f"        {prefix}_pending <= 1'b1;")          # 收到地址请求 → pending
...
lines.append(f"      end else if ({prefix}_pending && {data_valid} && {data_ready}) begin")
lines.append(f"        {prefix}_pending <= 1'b0;")          # 数据被取走 → 回 idle
```

对应的输出赋值（[scripts/pipeline/gen_tiny_stories_selftest_top.py:324-L326](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L324-L326)）把手写版的固定数据写法复现出来：

```python
lines.append(f"  assign {addr_ready} = ~{prefix}_pending;")  # 空闲时才接地址
lines.append(f"  assign {data_valid} = {prefix}_pending;")   # pending 时数据有效
lines.append(f"  assign {data} = {expr};")                   # 固定常数或地址回送
```

手写版（[rtl/tiny_stories_selftest_top.sv:60-L76](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/tiny_stories_selftest_top.sv#L60-L76)）就是它的具现：`in16_ld0_data = 64'h0123_4567_89AB_CDEF`，对应 `data_expr` 在「64 位 + 无地址」分支返回的常数（[scripts/pipeline/gen_tiny_stories_selftest_top.py:108-L109](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L108-L109)）。

**store 接收 + done token**——store 是「DUT 给载荷、外壳回确认」的反向握手。生成侧（[scripts/pipeline/gen_tiny_stories_selftest_top.py:332-L349](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L332-L349)）：

```python
lines.append(f"      if (!store_done_pending && {store_valid} && {store_ready}) begin")
lines.append(f"        store_done_pending <= 1'b1;")         # 接住载荷 → pending
lines.append(f"      end else if (store_done_pending && {store_done_ready}) begin")
lines.append(f"        store_done_pending <= 1'b0;")         # done token 被取走 → 回 idle
...
lines.append(f"  assign {store_ready} = ~store_done_pending;")
lines.append(f"  assign {store_done_valid} = store_done_pending;")
```

手写版（[rtl/tiny_stories_selftest_top.sv:82-L97](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/tiny_stories_selftest_top.sv#L82-L97)）与之逐行对应，通道编号是 `in17`。这个 done token 不是完成 token（完成 token 是 `out0`），而是 store 协议要求的「写完成回执」。

**pass/fail 监控**（生成侧 [scripts/pipeline/gen_tiny_stories_selftest_top.py:362-L388](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L362-L388)，手写版 [rtl/tiny_stories_selftest_top.sv:100-L127](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/tiny_stories_selftest_top.sv#L100-L127)）的核心判定：

```systemverilog
if (reset) begin
  pass_latched <= 1'b0;  fail_latched <= 1'b0;  cycle_count <= 32'd0;
end else if (!(pass_latched || fail_latched)) begin      // 一旦锁定就不再改
  if (out0_valid)                  pass_latched <= 1'b1; // done 在超时前拉起 → pass
  else if (cycle_count >= TIMEOUT_CYCLES) fail_latched <= 1'b1; // 超时 → fail
  else                             cycle_count  <= cycle_count + 32'd1;
end
```

`out0_valid` 即脚本里 `{done_valid}` 的具现。LED 映射固定不变（`[0]`=心跳、`[1]`=pass、`[2]`=fail），所以一块板上电后看 LED[1] 亮即 pass、LED[2] 亮即 fail、LED[0] 闪即电路在跑。

最后，DUT 例化把 `clock`/`reset` 接到外壳的 `SYS_CLK`/内部 `reset`，其余端口**同名直连**（生成侧 [scripts/pipeline/gen_tiny_stories_selftest_top.py:391-L401](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L391-L401)，手写版 [rtl/tiny_stories_selftest_top.sv:129-L147](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/tiny_stories_selftest_top.sv#L129-L147)）。任何没有被显式驱动的 DUT 输入，会被 `assign <name> = '0;` 兜底拉零（生成侧 [scripts/pipeline/gen_tiny_stories_selftest_top.py:353-L358](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L353-L358)），避免综合时出现悬空输入。

#### 4.3.4 代码实践

**实践目标**：把生成器的 load 驱动块与手写版逐行对齐，确认「生成器输出的就是手写版那段」。

**操作步骤**：

1. 把 [scripts/pipeline/gen_tiny_stories_selftest_top.py:284-L327](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L284-L327) 里 `prefix = "in16"` 代入，手工展开那几行 `lines.append(f"...")`（即把 `{prefix}` 全部换成 `in16`、`{addr_valid}` 换成 `in16_ld0_addr_valid`、…）。
2. 把展开结果与 [rtl/tiny_stories_selftest_top.sv:60-L76](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/tiny_stories_selftest_top.sv#L60-L76) 并排放。
3. 对 store 做同样的事：把 `store_prefix = "in17"` 代入生成侧 [scripts/pipeline/gen_tiny_stories_selftest_top.py:332-L349](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/gen_tiny_stories_selftest_top.py#L332-L349)，与手写版 [rtl/tiny_stories_selftest_top.sv:82-L97](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/tiny_stories_selftest_top.sv#L82-L97) 对照。

**需要观察的现象**：手工展开后的文本，与手写版对应段落的 `always_ff` / `assign` 在**语义上完全一致**（寄存器名手写版叫 `load_pending`，生成版叫 `in16_pending`，这是唯一的命名差异；逻辑一致）。

**预期结果**：load 块的状态机（`pending` 在 addr 握手时置 1、在 data 握手时清 0）与 store 块的状态机（`store_done_pending` 在 st0 握手时置 1、在 st0_done 握手时清 0）都能一一对应上。这就完成了「生成器 ≈ 手写版」的证明。

> 由于生成器当前未接入 `flake.nix`，最直接的验证是上一节末尾提到的「拿到 `main.sv` 后跑脚本 → diff 手写版」。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：pass 判据是「`out0_valid` 在超时前拉起」，而**不**是「比对推理结果数值」。为什么这样做是合理的工程取舍？

**答案**：TinyStories-1M 当前超配约 141 倍（u1-l4 / u5-l3），即便能综合也跑不到正确的完整推理；外壳的意义在于回答一个更基础的问题——「降级链产出的这片硬件能不能不死锁地跑完一次完整握手」。done 通道拉起就证明数据流贯通了，这已经是当前阶段（Task 3）能给出的有效验收。真正的数值等价性在 matmul 那条小核路线上由 u4-l2 的 Verilator testbench 负责。

**练习 2**：store 的 `st0_done_valid` 和完成的 `out0_valid` 都是「valid」，外壳对它们的处理有什么本质区别？

**答案**：`st0_done_valid` 是**外壳主动驱动**的输出（外壳作为 store 协议的接收方，在接住载荷后**回发**一个 done token 给 DUT），属于 store 握手协议的一部分；`out0_valid` 是**外壳观察**的输入（DUT 算完才会拉起），外壳只把它的 ready 常置 1 等结果，并用它做 pass 判据。一个是「我发的回执」，一个是「我等的完成」。

---

## 5. 综合实践

把本讲的三块知识串起来，完成下面这个端到端的小任务。

**任务**：以本仓库手写版 [rtl/tiny_stories_selftest_top.sv](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/tiny_stories_selftest_top.sv) 为「答案」，倒推 `gen_tiny_stories_selftest_top.py` 的行为，并指出二者的一处命名差异。

**步骤**：

1. **解析**（对应 4.1）：从手写版 DUT 例化块 [rtl/tiny_stories_selftest_top.sv:129-L147](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/tiny_stories_selftest_top.sv#L129-L147) 还原出 `main` 的端口表，列出每个端口的「方向 / 位宽」。注意 `in17_st0` 是 packed struct（位宽 `None`）。

2. **识别**（对应 4.2）：用 4.2 的四条启发式，写出脚本会选出的 start / done / store / load 通道编号。回答本讲标题对应的开放问题：
   - `choose_start_channel` 为什么选 `in\d+_valid` 中**编号最大**的？
   - `choose_done_channel` 为什么选 `out\d+_valid` 中**编号最小**的？
   - 用一句话说明这俩「取端值」规则的**共同风险**是什么。

3. **生成对照**（对应 4.3）：把手写版的 load 块（第 60–76 行）与生成器展开后的等价块对照，指出**唯一**的命名差异（`load_pending` vs `in16_pending`），并解释为什么这个差异不影响综合结果（同名信号在各自模块作用域内唯一即可）。

4. **结论**：写一段不超过 150 字的说明，回答「既然手写版已经在用，为什么还要维护 `gen_tiny_stories_selftest_top.py`？」——提示：从「`main.sv` 端口表会随降级链变化」出发。

**参考要点**：

- start 取最大 / done 取最小，编码的是 CIRCT lowering 对本模型「启动 token 排输入末尾、完成 token 排输出开头」的位置约定；共同风险是这是一条**经验规则**，换模型或换降级路径就可能误选，需要人工复核。
- 维护生成器的意义：`main.sv` 是构建产物，其端口表会随模型 / 补丁 / 外部化策略而变；手写外壳一旦端口对不上就综合失败。生成器让外壳能随端口表自动重派生，把「易错的机械接续」从人工维护里剥离出去（即便当前尚未接线，它也是这条自动化方向的设计凭据）。

## 6. 本讲小结

- 自测外壳 `tiny_stories_selftest_top` 把 `main` 包了一层，对外只暴露时钟、复位和 3 位 LED；所有 ESI 端口都是内部线网，由外壳驱动或观察。
- `gen_tiny_stories_selftest_top.py` 用「正则 + 方向继承状态机」把 `main.sv` 端口表解析成 `Port(name, direction, decl, bits)`；struct 位宽返回 `None` 触发 fail-fast。
- ESI 通道靠命名约定识别：start = 最大编号的成对 `in<N>_valid/ready`，done = 最小编号的成对 `out<N>_valid/ready`，store = 最小 `in<N>_st0*` 前缀，load = `in<N>_ld0_*` 四端口齐全者。
- 生成逻辑由 boot 复位、start 单脉冲、load（地址请求-数据响应）、store（接载荷-回 done token）、pass/fail 监控五块拼成；pass 判据是「done 在超时前拉起」，不做数值比对。
- 手写版 `rtl/tiny_stories_selftest_top.sv` 与生成器输出**结构等价**（命名上 `load_pending` 与 `in16_pending` 是唯一差异），目前 `flake.nix` 消费的是手写版，生成器是「自动重派生外壳」方向的设计凭据。
- 自动生成的根本动机是：`main.sv` 端口表会变，把易错的端口接续交给脚本而非人工。

## 7. 下一步学习建议

- 下一讲 **u6-l2 外部化超大 Handshake 存储** 会紧接着讲：当 `main` 内部的 Handshake 存储大到（≥128 kbit）必须当板载内存对待时，`externalize_large_memories.py` 如何把它们 blackbox 掉——这正是本讲外壳要同时应付 load/store 两类通道的深层原因（外部化后，DUT 通过 load/store 通道与外部存储通信）。
- 若想验证你对端口解析的理解，可阅读同目录的 `filter_rtlil_modules.py`（u5-l4 提到），它也是用正则处理 RTLIL 文本，与本讲思路同源。
- 想了解 store 通道里那个 packed struct（`address`+`data`）的语义来源，可回看 u3-l2 / u3-l3 关于 Handshake load/store 节点的降级讲解。
- 若你计划真的把生成器接进 `flake.nix`（替代手写版），需要先确认它对当前 `main.sv` 的输出与手写版 diff 为空（除命名外），这一步属于本讲「待本地验证」的延伸任务。
