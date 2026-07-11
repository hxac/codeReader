# 黄金参考与测试向量的生成

## 1. 本讲目标

降级链（PyTorch → torch-MLIR → CIRCT → SystemVerilog → Yosys）一共走了八九站，每一站都可能把语义跑偏。那么我们凭什么相信最后那块 FPGA/仿真出来的结果是对的？

本讲要回答的核心问题是：**在把模型降级成硬件之后，用什么标准去验证「硬件算出来的数」和「原始 PyTorch 模型算出来的数」完全一致？**

学完本讲，你应当能够：

1. 说清楚「黄金参考（golden reference）」在硬件验证里的角色，以及为什么 LLM2FPGA 选 PyTorch 作为**唯一真相源（single source of truth）**。
2. 读懂 `test_vectors.json` 里 `dtype`/`shape` 的强制校验逻辑，并解释这种「快速失败（fail-fast）」为什么必要。
3. 掌握「用 Python 生成 SystemVerilog 代码」这一技巧：`gen_tb_data.py` 如何把 PyTorch 跑出来的标量结果写成一段可被 `` `include `` 进 testbench 的 `tb_data.sv`。

本讲是单元 4（仿真与语义等价性验证）的第一讲，只负责**生产**验证基准；至于这个基准如何被 Verilator 仿真消费、如何驱动 valid/ready 握手、如何抓波形，留给下一讲 u4-l2。

---

## 2. 前置知识

本讲默认你已经读过 u1-l1（项目总路线）与 u3-l5（pipeline.nix 编排层），但下面几个概念先用大白话过一遍。

### 2.1 黄金参考（golden reference / golden model）

在硬件验证里，**被测设计（DUT, Design Under Test）** 就是那块从降级链里吐出来的 SystemVerilog/RTLIL；而 **黄金参考** 是一个「我们信任的计算同一件事的另一个实现」。把同一份输入同时喂给 DUT 和黄金参考，若两者输出一致，就认为 DUT 在这组输入上正确。

这里的关键是：**黄金参考必须独立于被测的东西**。如果黄金参考本身就是被测设计的产物，那么降级链里的 bug 会同时污染两边，比对永远「通过」，却毫无意义。本讲末尾会讲 LLM2FPGA 如何在 Nix 层保证这种独立性。

### 2.2 张量（tensor）、dtype 与点积

- **张量**：可以理解为多维数组。本讲里用到的是**一维**张量（向量），形状记作 `[16]`，即长度 16 的一维数组。
- **dtype**：张量里每个元素的数据类型。本讲固定用 `int32`（32 位有符号整数）。
- **torch.matmul 的点积语义**：当 `torch.matmul(a, b)` 的两个输入都是一维向量时，它计算的是**点积（内积）**，结果是一个标量：

\[
\text{result} = \sum_{i=0}^{n-1} a_i \cdot b_i
\]

### 2.3 SystemVerilog 的 `` `include `` 与 `initial` 块

- `` `include "tb_data.sv" `` 是**预处理指令**：在编译前，把 `tb_data.sv` 的文本原样粘贴到 testbench 里这一行所在的位置。
- `initial begin ... end` 块里的语句在仿真开始时（时刻 0）执行一次，常用来给存储器赋初值。

### 2.4 「唯一真相源」策略回顾

u1-l1 已经确立：整条降级链里，**PyTorch 模型是唯一真相源**，每个中间阶段都要和它对齐。本讲是把这条原则**操作化**的一讲——我们不再只是「相信」PyTorch 是对的，而是**亲手运行同一个 PyTorch 模型**，把它的输出固化成一个写死在 `.sv` 文件里的数字，让硬件去复现。

---

## 3. 本讲源码地图

本讲涉及的核心文件如下，按「数据从哪来 → 怎么算 → 怎么写出去」的顺序排列：

| 文件 | 角色 |
| --- | --- |
| [src/matmul.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul.py) | 被验证的模型本身（整条降级链的冒烟测试核 `MatmulModule`），也是黄金参考**复用**的同一份代码。 |
| [sim/test_vectors.json](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/test_vectors.json) | 固化的输入向量 `a`、`b`，以及它们的 dtype/shape 元信息。 |
| [sim/sim_utils.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/sim_utils.py) | 工具函数 `load_matmul_module`：按文件路径动态加载 `MatmulModule`，**与适配器共用同一个加载器**。 |
| [sim/gen_tb_data.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/gen_tb_data.py) | 本讲主角：读 JSON → 用 PyTorch 算 `expected` → 打印成 `tb_data.sv`。 |
| [sim/tb_main.sv](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/tb_main.sv) | testbench，`` `include `` 进 `tb_data.sv` 后用其中的 `a_mem`/`b_mem`/`expected` 完成比对（消费方，详细驱动见 u4-l2）。 |
| [flake.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix) | `tbDataSv` 派生把 `gen_tb_data.py` 的 stdout 重定向成 `tb_data.sv` 文件。 |

一句话定位：**`gen_tb_data.py` 是一条挂在主降级链旁边的「旁支」**，它只依赖 `src/matmul.py` 和测试向量，**完全不碰降级产物**——这正是黄金参考必须独立于 DUT 的工程体现。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **PyTorch 黄金参考**——为什么、怎么用同一个 `MatmulModule` 跑出 `expected`。
2. **`test_vectors.json` 校验**——为什么要强制校验 dtype 与 shape。
3. **生成 `tb_data.sv`**——把 Python 标量结果写成 SystemVerilog `initial` 块的代码生成技巧。

### 4.1 PyTorch 黄金参考

#### 4.1.1 概念说明

「等价性验证」最朴素也最可靠的思路是：**挑一组固定输入，用可信模型算出标准答案，再让被测设计去算同一个问题，两者逐位比对。** 这组「标准答案」就是黄金参考。

LLM2FPGA 选 PyTorch 当黄金参考，原因有二：

1. PyTorch 是整条降级链的**源头**（u1-l1 唯一真相源），它对 `matmul` 的语义是业界公认正确的；
2. 如果降级链忠实，那么「PyTorch 跑出来的数」和「硬件跑出来的数」在数学上应当**完全相等**（整数算术无舍入误差），比对可以做到逐位严格。

这里有一个**非常关键、容易忽略**的设计点：黄金参考和被测 DUT 必须复用**同一份模型源码**。LLM2FPGA 的做法是——`gen_tb_data.py`（黄金参考）和 `matmul_adapter.py`（喂给降级链的适配器，见 u2-l1）**调用同一个 `load_matmul_module()` 加载器**，从同一个 `src/matmul.py` 取出 `MatmulModule`。这样，两边用的是字面意义上的同一个 `forward`，不可能因为「黄金参考抄错了一版模型」而出现假通过。

> 小贴士：这就是为什么本讲反复强调「同一份代码」。如果黄金参考手写了一份 `matmul`，而降级链用的是另一份，两者的 bug 可能互相抵消或互相放大，验证就失去意义。

#### 4.1.2 核心流程

黄金参考的生成流程极短：

1. 从 `test_vectors.json` 读出固定输入 `a`、`b`（两个 `int32`、长度 16 的向量）。
2. 用 `load_matmul_module()` 加载 `MatmulModule`（与降级链同源）。
3. 在 `torch.no_grad()` 下调用 `m(a, b)`，得到标量结果，`.item()` 取出 Python 整数，记为 `expected`。
4. 这个 `expected` 就是「硬件必须复现的那个数」。

用伪代码表示：

```
a, b = load_vectors(test_vectors.json)     # 固定输入
MatmulModule = load_matmul_module()        # 与 DUT 同源
expected = MatmulModule().eval()(a, b).item()
# expected 之后会被写进 tb_data.sv
```

`expected` 的数学含义就是点积：

\[
\text{expected} = \sum_{i=0}^{15} a_i \cdot b_i
\]

#### 4.1.3 源码精读

先看共享加载器 `sim_utils.py`：[sim/sim_utils.py:8-16](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/sim_utils.py#L8-L16)。这段代码用 `importlib` 按文件路径动态加载 `matmul.py`，并返回其中的 `MatmulModule` 类：

```python
def load_matmul_module():
    default_matmul_path = Path(__file__).parent.parent / "src" / "matmul.py"
    matmul_path = Path(os.environ.get("MATMUL_PY", str(default_matmul_path)))
    ...
    return module.MatmulModule
```

要点：

- 默认指向仓库里的 `src/matmul.py`，但可以被环境变量 `MATMUL_PY` 覆盖——这一点在 4.3 节讲 Nix 时会用到，让 Nix 把加载目标**钉死**成某个具体路径。
- 这正是 `matmul_adapter.py` 第 5、8 行（[src/matmul_adapter.py:5](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py#L5)）调用的同一个函数，保证「黄金参考」与「喂给降级链的模型」是同一份代码。

再看 `MatmulModule` 本体：[src/matmul.py:3-5](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul.py#L3-L5)。它极其简单：

```python
class MatmulModule(torch.nn.Module):
    def forward(self, a, b):
        return torch.matmul(a, b)
```

这就是被验证的全部「业务逻辑」——两个一维向量做点积。

最后看 `gen_tb_data.py` 里算 `expected` 的四行：[sim/gen_tb_data.py:24-27](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/gen_tb_data.py#L24-L27)：

```python
MatmulModule = load_matmul_module()
m = MatmulModule().eval()
with torch.no_grad():
    expected = int(m(a, b).item())
```

- `.eval()` 把模块切到推理模式（对 matmul 行为无影响，但与适配器保持一致的好习惯，见 u2-l1）。
- `torch.no_grad()` 关闭 autograd，因为这里只需要前向结果、不需要梯度。
- `m(a, b)` 返回一个 0 维（标量）`int32` 张量；`.item()` 取出 Python 整数；外层 `int(...)` 是显式兜底，确保类型一定是 `int`，便于后面拼进字符串。

#### 4.1.4 代码实践

**实践目标**：亲手算出 `expected`，验证它确实是 PyTorch 会给出的那个数，建立「黄金参考可被独立复核」的信心。

**操作步骤**：

1. 打开 [sim/test_vectors.json](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/test_vectors.json)，读出 `a = [1, 2, …, 16]`，`b = [16, 15, …, 1]`。
2. 按点积公式手算：

\[
\text{expected} = \sum_{i=0}^{15} a_i b_i = 1{\cdot}16 + 2{\cdot}15 + \cdots + 16{\cdot}1
\]

   利用对称性（`a[i] + b[i] = 17` 恒成立）：

\[
\text{expected} = \sum_{i=1}^{16} i(17-i) = 17\sum_{i=1}^{16} i - \sum_{i=1}^{16} i^2 = 17 \times 136 - 1496 = 816
\]

**需要观察的现象 / 预期结果**：`expected = 816`。下一节你会看到这个 `816` 被写进 `tb_data.sv` 的 `expected = 32'd816;`，并最终在 `tb_main.sv` 里和硬件输出做 `!==` 比较。

> 说明：上述 816 完全由源码与向量推导得出，并非运行结果。如果你想本地复核，可在带 torch 的环境里执行（待本地验证）：
>
> ```bash
> python -c "import torch; a=torch.tensor(list(range(1,17)),dtype=torch.int32); b=torch.tensor(list(range(16,0,-1)),dtype=torch.int32); print(torch.matmul(a,b).item())"
> ```

#### 4.1.5 小练习与答案

**练习 1**：对 `MatmulModule` 这种纯算子，`.eval()` 和 `torch.no_grad()` 其实都不改变结果，为什么 `gen_tb_data.py` 还要写它们？

**参考答案**：这是与适配器（`matmul_adapter.py` 里 `.eval()`、u2-l1 的导出流程）保持一致的工程习惯；同时 `torch.no_grad()` 避免构建不必要的计算图、省内存。对 matmul 确实无行为差异，但写成这样能让「黄金参考的调用方式」与「降级链消费模型的方式」尽可能对齐，减少「两边调用姿势不同导致假通过」的风险。

**练习 2**：如果把 `test_vectors.json` 的 dtype 改成 `float32`，整条等价性验证会受到什么影响？（提示：联系 u3-l4 / u6-l3 的浮点 extern。）

**参考答案**：浮点路径会触发 CIRCT 把浮点算子降级成 extern 黑盒（u3-l4），最终用 `circt_fp_primitives.sv` 的 Q16.16 定点近似实现（u6-l3）。此时 PyTorch 的 float32 结果与硬件定点近似结果**不再逐位相等**，`!==` 这种逐位比对会几乎必然失败——这正是 matmul 冒烟测试刻意选用 `int32` 的原因：把「降级链结构性正确性」与「浮点近似误差」这两个问题解耦，先用整数验证前者。

---

### 4.2 `test_vectors.json` 校验

#### 4.2.1 概念说明

`test_vectors.json` 是一份**写死的、版本化的输入数据**。它有两个作用：

1. **可复现**：输入固定，黄金参考和仿真结果就都可复现。如果用随机数，每次跑出来 `expected` 都不一样，难以排查回归。
2. **契约**：它同时声明了「数据长什么样」（dtype、shape），让消费它的代码可以提前校验，而不是拿到错数据后跑出一堆莫名其妙的数。

「快速失败（fail-fast）」的意思是：**一旦输入和预期不符，立刻在最前面抛错，绝不把错误数据往下游传。** 这比「让错误数据安静地流过整条链、最后给出一个看起来正常其实全错的结果」要安全得多。

#### 4.2.2 核心流程

`load_vectors` 的流程：

1. 打开 JSON，解析成字典 `payload`。
2. 检查 `payload["dtype"] == "int32"`，否则抛错。
3. 检查 `payload["shape"] == [16]`，否则抛错。
4. 把 `payload["a"]`、`payload["b"]` 转成 `torch.int32` 张量。
5. **再**检查每个张量元素数 `numel() == 16`，否则抛错。
6. 返回 `(a, b)`。

注意第 3 步和第 5 步看似重复（shape 已经是 `[16]` 了，为何还查 `numel() == 16`？）——这是**纵深防御**：JSON 里的 `shape` 字段只是「自报家门」的元信息，可能和实际数组长度对不上；`numel()` 查的是真实张量的元素数。两道闸门各查一样东西。

#### 4.2.3 源码精读

校验逻辑全在 [sim/gen_tb_data.py:8-19](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/gen_tb_data.py#L8-L19)：

```python
def load_vectors(path: Path) -> tuple[torch.Tensor, torch.Tensor]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if payload.get("dtype") != "int32":
        raise ValueError(f"Expected dtype 'int32', got {payload.get('dtype')!r}")
    if payload.get("shape") != [16]:
        raise ValueError(f"Expected shape [16], got {payload.get('shape')!r}")
    a = torch.tensor(payload["a"], dtype=torch.int32)
    b = torch.tensor(payload["b"], dtype=torch.int32)
    if a.numel() != 16 or b.numel() != 16:
        raise ValueError("Expected exactly 16 elements in both 'a' and 'b'")
    return a, b
```

逐条对应：

- [第 11-12 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/gen_tb_data.py#L11-L12)：dtype 必须是 `int32`。`payload.get('dtype')` 用 `.get` 而非 `[]`，这样字段缺失时得到 `None`，也会落入这条报错，不会 `KeyError`。
- [第 13-14 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/gen_tb_data.py#L13-L14)：shape 必须恰好是 `[16]`。注意是比较 Python 列表 `[16]`，所以 `[16, 1]`、`[1, 16]` 都会被拒。
- [第 15-16 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/gen_tb_data.py#L15-L16)：**显式**用 `dtype=torch.int32` 构造张量——即使 JSON 元素是整数，也强制转成 int32，避免上游误塞浮点数后被 torch 推断成 float。
- [第 17-18 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/gen_tb_data.py#L17-L18)：真实元素数校验，是 shape 校验的「兜底」。

对照 [sim/test_vectors.json:2-6](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/test_vectors.json#L2-L6)，可以看到 `"dtype": "int32"` 与 `"shape": [16]` 正好满足上面两条；而 [a 数组](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/test_vectors.json#L7-L24) 与 [b 数组](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/test_vectors.json#L25-L42) 各 16 个元素，满足 numel 校验。

#### 4.2.4 代码实践

**实践目标**：通过「故意破坏」`test_vectors.json`，观察校验如何在前端拦截错误，体会 fail-fast 的价值。

**操作步骤**：复制一份 `test_vectors.json` 到临时位置（**不要改原文件**，原文件被 Nix 派生指纹依赖），分别做三种破坏并运行 `python sim/gen_tb_data.py`（待本地验证）：

1. 把 `"dtype": "int32"` 改成 `"dtype": "float32"`。
2. 把 `"shape": [16]` 改成 `"shape": [4, 4]`。
3. 把 `a` 数组删掉一个元素（变 15 个）。

**需要观察的现象 / 预期结果**：根据源码，三种情况分别抛出对应的 `ValueError`，例如第 1 种会得到 `ValueError: Expected dtype 'int32', got 'float32'`；第 3 种会先通过 dtype/shape 两关，再被 `numel()` 闸门拦下：`ValueError: Expected exactly 16 elements in both 'a' and 'b'`。**注意第 3 种尤其能说明「shape 自报家门」与「真实长度」是两件事——shape 字段没动却仍被 numel 抓住。**

#### 4.2.5 小练习与答案

**练习 1**：`load_vectors` 已经查了 `shape == [16]`，为什么还要再查 `numel() == 16`？这不是重复吗？

**参考答案**：不重复。`shape` 是 JSON 里**作者手写的元信息**，完全可能与实际数组长度不一致（例如有人把数组删短了却忘了改 shape）。`numel()` 查的是 `torch.tensor(...)` 构造出来的**真实**张量元素数。前者查「声明」，后者查「事实」，两道闸门覆盖两种出错方式。

**练习 2**：校验里为什么用 `payload.get("dtype")` 而不是 `payload["dtype"]`？

**参考答案**：`.get` 在键缺失时返回 `None`，会顺畅地落入 `!= "int32"` 分支并给出清晰的 `ValueError`；而 `payload["dtype"]` 在键缺失时会抛 `KeyError`，错误信息对用户不友好（看不出「期望 int32」这层语义）。这是一种让报错信息更有诊断价值的写法。

---

### 4.3 生成 `tb_data.sv`

#### 4.3.1 概念说明

黄金参考最终要被 SystemVerilog 的 testbench 用到，但 SystemVerilog 没法直接 `import torch`。解决办法是**代码生成（code generation）**：用 Python 把数字**拼成一段 SystemVerilog 文本**，写进一个 `.sv` 文件，再让 testbench `` `include `` 它。

`gen_tb_data.py` 生成的 `tb_data.sv` 包含三样东西：

1. 两个存储器声明：`a_mem`、`b_mem`（各 16 个 32 位字），用来装载输入向量；
2. 一个常量 `expected`（32 位），即黄金参考标量；
3. 一个 `initial` 块，把 `a`、`b` 的每个元素塞进对应存储器。

这是一段「数据驱动的代码」：文件内容随 `test_vectors.json` 和 PyTorch 结果而变，但**骨架固定**。

#### 4.3.2 核心流程

`main` 的流程（[sim/gen_tb_data.py:21-37](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/gen_tb_data.py#L21-L37)）：

1. 调 `load_vectors` 拿到 `a`、`b`。
2. 用 PyTorch 算 `expected`（见 4.1）。
3. `print` 两行存储器声明 + 一行 `expected` 声明。
4. `print "initial begin"`。
5. 循环 16 次，`print` 每个 `a_mem[i] = 32'd...;`；再循环 16 次 `b_mem[i]`。
6. `print "end"`。

关键点：**所有输出都走 `print`（标准输出）**，脚本本身不写文件。把 stdout「重定向成文件」是调用方（Nix）的职责——这让脚本保持「Unix 小工具」风格，既能被 `> tb_data.sv` 重定向，也能直接 `| less` 查看。

#### 4.3.3 源码精读

打印 SV 的核心在 [sim/gen_tb_data.py:29-37](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/gen_tb_data.py#L29-L37)：

```python
print("logic [31:0] a_mem [0:15];")
print("logic [31:0] b_mem [0:15];")
print(f"logic [31:0] expected = 32'd{expected};")
print("initial begin")
for i in range(16):
    print(f"  a_mem[{i}] = 32'd{int(a[i])};")
for i in range(16):
    print(f"  b_mem[{i}] = 32'd{int(b[i])};")
print("end")
```

逐行解读：

- `logic [31:0] ... [0:15]`：声明一个 16 深、每字 32 位的存储器，正好对应 int32 × 16。
- `32'd{expected}`：SystemVerilog 的「32 位十进制字面量」写法，`expected`（Python 整数 816）被插值成 `32'd816`。
- `for i in range(16)`：循环展开 16 行赋值。`int(a[i])` 把张量元素转回 Python 整数再插值。

把 4.1 节算出的 `expected = 816`、`a = [1..16]`、`b = [16..1]` 代入，生成的 `tb_data.sv` 形如（**示例代码，由源码逻辑推导得出**）：

```systemverilog
logic [31:0] a_mem [0:15];
logic [31:0] b_mem [0:15];
logic [31:0] expected = 32'd816;
initial begin
  a_mem[0] = 32'd1;
  a_mem[1] = 32'd2;
  // ... a_mem[15] = 32'd16;
  b_mem[0] = 32'd16;
  // ...
  b_mem[15] = 32'd1;
end
```

**消费方**：`tb_main.sv` 第 4 行 `` `include "tb_data.sv" `` 把这段文本粘进 testbench（[sim/tb_main.sv:4](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/tb_main.sv#L4)）；其 [第 9-12 行注释](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/tb_main.sv#L9-L12) 明确写了 `expected is the value calculated by simulating the PyTorch module, therefor being our gold reference`——这正是本讲建立的概念在源码里的落点。

`a_mem`/`b_mem` 在 testbench 里被用来**直接播种硬件内部存储**：[sim/tb_main.sv:63-66](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/tb_main.sv#L63-L66)，而 `expected` 在 [sim/tb_main.sv:82-90](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/tb_main.sv#L82-L90) 与硬件输出 `in2_st0_data` 做 `!==` 比较，打印 `PASS`/`FAIL`。这两段是 u4-l2 的重点，本讲只需理解「`tb_data.sv` 提供了三个被消费的量」。

**Nix 编排**：[flake.nix:703-709](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L703-L709) 的 `tbDataSv` 派生把 stdout 重定向成文件：

```nix
tbDataSv = pkgs.runCommand "tb-data-sv" { } ''
  mkdir -p "$out"
  MATMUL_PY=${./src/matmul.py} \
  ${pythonWithTorch}/bin/python ${./sim}/gen_tb_data.py > "$out/tb_data.sv"
'';
```

两个细节呼应前文：

- `MATMUL_PY=${./src/matmul.py}`：把 `sim_utils.load_matmul_module` 的加载目标**钉死**成 Nix store 里这份 `matmul.py`（4.1.3 节提到的环境变量覆盖在这里生效）。这正是「黄金参考与 DUT 同源」在 Nix 层的硬保证——两者都源自同一份被内容寻址的 `src/matmul.py`。
- `> "$out/tb_data.sv"`：把 4.3.2 节所说的「stdout 重定向成文件」落实。`tb_main.sv` 注释里提到可用 `nix build .#tb-data-sv` 单独构建它。

最后回到「独立性」：这个 `tbDataSv` 派生**只依赖 `src/matmul.py` 与 `sim/` 下的脚本**，依赖图里**没有任何降级产物**（不依赖 `sv`、`il` 等阶段）。也就是说，哪怕整条降级链全坏了，黄金参考照样能独立算出正确的 `816`——这正是 2.1 节强调的「黄金参考必须独立于 DUT」在依赖图上的体现。

#### 4.3.4 代码实践

**实践目标**：亲手生成 `tb_data.sv`，确认它的内容与 4.1/4.2 的推导一致，并追踪它的三个量如何流进 testbench。

**操作步骤**：

1. 在仓库根目录运行（待本地验证，需要带 torch 的环境；Nix 用户可直接 `nix build .#tb-data-sv`）：
   ```bash
   python sim/gen_tb_data.py > /tmp/tb_data.sv
   ```
2. 打开 `/tmp/tb_data.sv`，核对：
   - `expected = 32'd816;`（与 4.1 手算一致）；
   - `a_mem[0]..a_mem[15]` 依次是 `1..16`，`b_mem[0]..b_mem[15]` 依次是 `16..1`。
3. 打开 [sim/tb_main.sv](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/tb_main.sv)，找到 [第 63-66 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/tb_main.sv#L63-L66) 的 `a_mem[i]`/`b_mem[i]` 被赋给 `dut.handshake_memory0...` / `dut.handshake_memory1...`，确认 `a_mem`、`b_mem` 的「生产者（tb_data.sv）→ 消费者（tb_main.sv 播种）」链路是通的。

**需要观察的现象 / 预期结果**：`tb_data.sv` 顶部三行声明完全可预测（`expected` 为 816）；testbench 里的存储器名 `a_mem`/`b_mem` 与生成的声明**逐字一致**——如果两边名字对不上，`` `include `` 后会编译失败。这条「跨文件的命名契约」完全靠 `gen_tb_data.py` 与 `tb_main.sv` 人工对齐，没有编译期类型检查兜底，所以改动任一边都要同步改另一边。

> 说明：以上输出格式由源码 `print` 语句与已知向量严格推导；实际运行只需复核数字，行为可预测。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `tb_data.sv` 用 `logic [31:0] a_mem [0:15]` 而不是 `logic [15:0] a_mem`？

**参考答案**：`[31:0]` 是**每个字**的位宽（32 位，对应 int32），`[0:15]` 是**存储器深度**（16 个字）。二者维度不同：前者描述「一个元素多宽」，后者描述「有多少个元素」。写成 `logic [15:0] a_mem` 是单个 16 位信号，既位数不对也存不下 16 个值。

**练习 2**：`gen_tb_data.py` 只负责 `print`，从不 `open(..., 'w')` 写文件。这种设计有什么好处？

**参考答案**：把「生成内容」和「落到哪个文件」解耦。脚本只管往 stdout 吐文本，调用方（Nix 的 `> "$out/tb_data.sv"`、或人手的 `| less` / 重定向）决定它去哪。这是 Unix 小工具的惯用法：单一职责、可组合、便于调试时直接看输出。

---

## 5. 综合实践

把三个模块串起来，完成一次「换输入 → 重算黄金参考 → 核对生成产物 → 追踪消费」的完整闭环。

**任务**：假设你想把测试向量换成 `a = [2, 4, 6, …, 32]`（偶数 2..32）、`b = [1, 1, 1, …, 1]`（16 个 1），请完成下列步骤：

1. **算 expected**：手算点积。`b` 全 1，所以 `expected = 2+4+…+32 = 2·(1+2+…+16) = 2·136 = 272`。
2. **改 JSON（在副本上）**：把 `a` 改成上述偶数、`b` 改成全 1，保持 `dtype=int32`、`shape=[16]` 不变。
3. **预测 `tb_data.sv`**：写出新的 `expected` 行（应为 `32'd272;`），以及 `a_mem[0] = 32'd2;`、`b_mem[0] = 32'd1;` 等。
4. **运行核对**（待本地验证）：`python sim/gen_tb_data.py`，确认输出与你的预测一致。
5. **追踪契约**：在 [sim/tb_main.sv](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/tb_main.sv) 中指出，新的 `a_mem`/`b_mem` 会经 [第 63-66 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/tb_main.sv#L63-L66) 播种进 DUT 的两个 handshake memory，新的 `expected` 会在 [第 82-90 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/tb_main.sv#L82-L90) 被拿来和硬件输出比对。

**预期结果**：你能独立地把「输入数据 → PyTorch 黄金参考 → SystemVerilog 初始化代码 → testbench 比对点」整条链走通，并解释链上每一步为什么必须与前一步对齐（dtype、shape、命名）。这也说明：**更换测试向量不需要改任何模型或降级脚本，只动 `test_vectors.json` 一处**——这正是把输入数据与验证逻辑解耦的好处。

---

## 6. 本讲小结

- **黄金参考**用「同一个 PyTorch 模型跑同一组输入」得到标准答案；`gen_tb_data.py` 与降级链适配器共用 `load_matmul_module()`，保证参考与 DUT **同源**。
- `test_vectors.json` 把输入**固化+版本化**，并用 dtype/shape/numel 三重校验实现 **fail-fast**，绝不让坏数据流到下游。
- `expected` 由 `torch.matmul`（两个一维向量即点积）算出，本讲示例下为 **816**。
- `gen_tb_data.py` 用「Python 拼字符串」生成 SystemVerilog 的 `initial` 块，所有输出走 stdout，由 Nix 的 `tbDataSv` 派生重定向成 `tb_data.sv`。
- **独立性**：`tbDataSv` 派生只依赖 `src/matmul.py` 与 `sim/` 脚本，不依赖任何降级产物——降级链坏了，黄金参考依然正确，这正是等价性验证可信的根基。
- matmul 冒烟测试刻意用 **int32**，把「降级链结构性正确性」与「浮点近似误差」两个问题解耦。

---

## 7. 下一步学习建议

本讲只生产了「标准答案」（`tb_data.sv`），还没真正用它去跑仿真。下一讲 **u4-l2 Verilator 仿真与波形捕获** 会接上消费侧：

- 看 `tb_main.sv` 如何驱动 DUT 的 valid/ready 握手、如何用层次化引用 `dut.handshake_memory0._handshake_memory_5[i]` 直接播种内部存储；
- 看 `flake.nix` 里 `matmulSvSim`（`--binary --timing`）与 `matmulSvWave`（`--trace`、`ENABLE_WAVES_VCD`）两个派生如何把 Verilator 跑起来并抓 VCD 波形；
- 在波形里亲眼看到 `in2_st0_data` 从未定义变成 `816`、与 `expected` 比对后打印 `PASS` 的那一刻。

建议先行重读 u3-l5 中 `runCommand` 派生的依赖关系一节，以便把 `tbDataSv`（本讲）与 `simMain`/`matmulSvSim`（u4-l2）放进同一张依赖图理解。如果想往专家层延伸，可对比本讲的「整数逐位比对」与 u6-l3 浮点近似下「无法逐位比对」的验证难题，体会 dtype 选择对验证策略的根本影响。
