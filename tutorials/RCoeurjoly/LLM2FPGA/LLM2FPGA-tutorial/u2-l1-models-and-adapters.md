# 模型与适配器契约

## 1. 本讲目标

经过第一单元，你已经能执行 `nix build .#tiny-stories-1m-baseline-float-...` 把整条降级链跑通，并读懂资源报告里的「超配约 141 倍」。但那只回答了「能不能跑」。从本单元开始，我们要沿着数据流的方向，一节一节地拆开这条链，看清楚**每一站的输入到底是什么、谁产生它、又以什么格式交给下一站**。

本讲（u2-l1）位于 PyTorch 前端的最入口。学完之后你应该能够：

- 理解 LLM2FPGA 为什么把「**模型本身**」和「**适配器（adapter）**」分成两层来写。
- 准确说出适配器必须实现的契约：`build_model(model_path)` 和 `example_inputs()`，以及可选的 `EXPORT_STRICT` 标志。
- 对照最小核 `MatmulModule` 与真实 `TinyStories-1M` 适配器，看出「一个只做一次乘加的小模型」和「一个真正的 HuggingFace 大模型」在接入流水线时的差异与共同点。
- 自己动手为一个新模块（例如 `torch.add`）写出一个合法的适配器。

本讲只涉及 PyTorch 侧的代码，**不会**深入 torch-MLIR 的降级细节——那是下一讲（u2-l2）的内容。

## 2. 前置知识

在进入源码之前，先用最直白的话把几个概念交代清楚。如果你已经熟悉，可以跳到第 3 节。

### 2.1 什么是 torch.nn.Module

PyTorch 里几乎所有的模型都继承自 `torch.nn.Module`。一个 `Module` 本质上做一件事：定义 `forward(self, ...) -> 输出`，也就是「给我输入张量，我算出输出张量」。模型里的权重（weights）是 `Module` 的成员变量，框架会自动追踪它们。

> 一句话：**`torch.nn.Module` = 带权重的 `forward` 函数。**

### 2.2 什么是 torch.export（图捕获）

平时我们调用 `model(a, b)`，PyTorch 是「边执行边解释」（这叫 eager 模式）。但要把模型喂给一条编译器链，编译器需要的是一张**静态的计算图**——节点是算子，边是张量。

`torch.export.export(model, (a, b))` 的作用就是：拿一组「示例输入」`(a, b)` 喂给模型跑一遍，把这次执行追踪下来，固化成一张可序列化的 `ExportedProgram` 图。这张图随后才能被 torch-MLIR 转成 MLIR。所以：

> **示例输入（example inputs）不只是「跑一下试试」，它是把模型变成计算图的「模具」。**

这正是后面反复强调「`example_inputs()` 的形状必须和 `build_model` 的输入一致」的原因——模具不匹配，就脱不出正确的图。

### 2.3 什么是 lowering（降级）与 dialect（方言）

- **Lowering（降级）**：把高层的、抽象的表示一步步翻译成更底层、更接近硬件的表示。本项目的整条链就是一次漫长的 lowering。
- **Dialect（方言）**：MLIR 里的「一组算子集合」。比如 `torch` 方言里有 `torch.aten.matmul`，`linalg` 方言里有结构化循环算子。降级就是「从一种方言翻译到另一种方言」。

本讲只涉及最前端：把一个 `nn.Module` 变成 **torch 方言** 的 MLIR 文本。后面的 `torch → linalg → cf → handshake → hw → sv` 一长串，都建立在这个产物之上。

### 2.4 matmul 为什么是「最小核」

矩阵乘法（matmul）是 LLM 里最重的算子——无论是注意力里的 QKV 相乘，还是 MLP 里的线性层，归根结底都是大规模 matmul。团队选它做「最小核（minimal kernel）」有两个原因：

1. 它足够简单，`torch.matmul` 几乎一定被工具链支持；
2. 它又足够关键，能逼着整条链（一直到 SystemVerilog 和仿真）真正跑通一遍。

所以 `matmul` 是一块**贯穿全局的试金石**：先用它证明「这条路通」，再去啃真正的 TinyStories-1M。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 在本讲中的角色 |
|------|------|----------------|
| [src/matmul.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul.py) | 定义 `MatmulModule`：只做一次 `torch.matmul` | 「模型本身」的最小例子 |
| [sim/sim_utils.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/sim_utils.py) | `load_matmul_module()`：按文件路径动态加载 `matmul.py` | 连接「模型」与「适配器」的胶水 |
| [src/matmul_adapter.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py) | matmul 适配器：实现 `build_model` / `example_inputs` | 最小适配器样本 |
| [TinyStories/model_adapter.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/TinyStories/model_adapter.py) | TinyStories-1M 适配器：从 HuggingFace 加载真模型 | 真实大模型适配器样本 |
| [scripts/compile-pytorch.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py) | 契约的**消费者**：加载适配器、导出图、转 MLIR | 定义并强制执行契约 |
| [nix/models.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix) | 把适配器注册成 Nix 派生 | 适配器如何被流水线调用 |
| [flake.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix)（200–225 行） | 把 HuggingFace 模型钉成本地快照 | `--model-path` 的来源 |

> **关键认知**：本讲里**契约的权威定义不在适配器里，而在 `compile-pytorch.py` 里**。适配器是「实现方」，`compile-pytorch.py` 是「验收方」。理解契约，要先看验收方。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 MatmulModule 最小核**——看一个「模型本身」长什么样。
- **4.2 适配器接口契约**——看适配器必须满足什么约定，以及谁来强制执行。
- **4.3 HuggingFace 模型加载与 export 选项**——看真实大模型适配器与最小核的差异。

### 4.1 MatmulModule 最小核

#### 4.1.1 概念说明

「最小核」的意思是：一个尽可能简单、却又能代表整条链关键算子的 PyTorch 模型。`MatmulModule` 就是这样的核——它继承 `torch.nn.Module`，唯一的 `forward` 只调用一次 `torch.matmul`。

把它和真实 LLM 做个对比：

| | `MatmulModule` | 真实 TinyStories-1M |
|---|---|---|
| 算子种类 | 1 个（matmul） | 几十种（matmul、softmax、LayerNorm、GELU、embedding…） |
| 是否有权重 | 否 | 是（约 100 万参数） |
| 是否需要从外部加载 | 否（纯算子） | 是（HuggingFace 权重文件） |
| 是否能验证全链路 | 能，而且便宜 | 能，但贵且容易触发工具链 bug |

正因为 `MatmulModule` 没有权重、没有动态形状、没有数据相关的控制流，它成了整条降级链的「冒烟测试（smoke test）」——每次改动工具链，都先拿它跑一遍。

#### 4.1.2 核心流程

`MatmulModule` 的执行就是一句话：

```text
输入张量 a, b  ──forward()──>  torch.matmul(a, b)  ──>  输出张量
```

注意本讲里 `a`、`b` 都是长度为 16 的一维 `int32` 张量（见 [src/matmul_adapter.py:16-19](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py#L16-L19)）。对两个一维张量，`torch.matmul` 退化为**点积（dot product）**，输出是一个标量（0 维张量）：

\[
\text{out} = \sum_{i=0}^{15} a_i \cdot b_i
\]

也就是说，这个「矩阵乘核」在本讲的配置下其实算的是 16 维向量的内积。这是为了让仿真（见 u4-l1）的黄金参考足够简单——一个标量最好比对。若改成二维输入，`torch.matmul` 就会变成真正的矩阵乘。

#### 4.1.3 源码精读

整个模型只有 6 行：

[src/matmul.py:1-6](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul.py#L1-L6) —— 定义最小核 `MatmulModule`：

```python
import torch

class MatmulModule(torch.nn.Module):
    def forward(self, a, b):
        return torch.matmul(a, b)
```

这段代码做了三件事：

1. `import torch`：引入 PyTorch。
2. 继承 `torch.nn.Module`：让它成为一个合法的 PyTorch 模型（可以被 `torch.export` 捕获）。
3. `forward(self, a, b)`：定义输入是两个张量、输出是它们的 matmul。

注意：**这里没有 `__init__`、没有任何 `nn.Parameter`**，说明这个模型没有任何可训练权重——它是一个纯算子包装。这也意味着它不需要从磁盘加载权重文件。

但是 `matmul.py` 与适配器是分开存放的（一个在 `src/`，适配器在 `src/matmul_adapter.py`），适配器要怎么拿到 `MatmulModule` 这个类？答案在 `sim_utils.py`：

[sim/sim_utils.py:8-16](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/sim_utils.py#L8-L16) —— 按文件路径动态加载 `MatmulModule`：

```python
def load_matmul_module():
    default_matmul_path = Path(__file__).parent.parent / "src" / "matmul.py"
    matmul_path = Path(os.environ.get("MATMUL_PY", str(default_matmul_path)))
    spec = importlib.util.spec_from_file_location("matmul_module", matmul_path)
    ...
    spec.loader.exec_module(module)
    return module.MatmulModule
```

这段是标准的「按文件路径加载 Python 模块」写法（`importlib.util`）。有两点值得记住：

- **默认路径**是 `src/matmul.py`（相对 `sim_utils.py` 往上两级再进 `src/`）。
- **可用环境变量 `MATMUL_PY` 覆盖**。这个覆盖能力是 Nix 在 [nix/models.nix:11-13](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L11-L13) 里用上的：`export MATMUL_PY="${matmulPy}"`，把 Nix store 里那份确定版本的 `matmul.py` 钉死，保证可复现。

#### 4.1.4 代码实践

**实践目标**：确认「最小核」确实是纯算子、无权重，并理解 `MATMUL_PY` 覆盖机制。

**操作步骤**（源码阅读型实践）：

1. 打开 [src/matmul.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul.py)，确认里面没有 `__init__`、没有 `self.xxx = nn.Parameter(...)`。
2. 打开 [sim/sim_utils.py:8-16](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/sim_utils.py#L8-L16)，找到读 `MATMUL_PY` 的那一行。
3. 打开 [nix/models.nix:11-13](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L11-L13)，看到 `export MATMUL_PY="${matmulPy}"` 这一行。

**需要观察的现象**：

- `matmul.py` 内确实不含任何权重定义。
- `sim_utils.py` 用 `os.environ.get("MATMUL_PY", <默认>)` 决定从哪个文件加载。
- Nix 在调用前用 `export` 把环境变量设成了 store 路径。

**预期结果**：你能用一句话解释——「`matmul.py` 是纯算子模型，`sim_utils` 提供按路径加载能力，Nix 借 `MATMUL_PY` 把加载路径钉死以保可复现」。

> 待本地验证：若你在 `nix develop` 里手动 `MATMUL_PY=/tmp/my_matmul.py python -c "from sim_utils import load_matmul_module; print(load_matmul_module())"`，应该看到它加载的是你指定的那份文件（取决于你的 `my_matmul.py` 是否定义了 `MatmulModule`）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `MatmulModule` 改成 `class MatmulModule: ...`（不继承 `torch.nn.Module`），会发生什么？

**答案**：`torch.export.export` 会拒绝它，因为 export 的对象必须是 `torch.nn.Module`（或可调用且能被追踪的对象）。不继承 `nn.Module`，后续整条链就起不了步。

**练习 2**：`load_matmul_module()` 为什么用 `importlib.util` 这种「按文件路径加载」的方式，而不是直接 `from src.matmul import MatmulModule`？

**答案**：为了让 `MATMUL_PY` 环境变量能在运行时替换成任意一份 `matmul.py`（例如 Nix store 里的版本），而普通的 `import` 语句路径是写死的、且依赖 `sys.path` 里有 `src/`。按文件加载 = 路径完全由环境变量决定，更适合可复现的构建。

---

### 4.2 适配器接口契约

#### 4.2.1 概念说明

现在模型本身（`MatmulModule`）有了。但流水线不能直接认识 `MatmulModule`，也不能直接认识 HuggingFace 的 `AutoModelForCausalLM`——它需要一个**统一的入口**，无论什么模型，都按同一种方式被「装配」。

这个统一入口就是**适配器（adapter）**。你可以把它理解成一个「翻译层」或「插头」：

> **适配器 = 一个普通 Python 文件，对外暴露几个约好名字的函数，把「任意模型」包装成「流水线认识的标准件」。**

「模型」与「适配器」分离的好处很直接：

- 模型该怎么写还怎么写（`matmul.py` 干干净净，只有算子；TinyStories 直接用上游权重）。
- 流水线只和适配器打交道，永远只调用 `build_model` / `example_inputs` 这两个名字，不必关心模型内部细节。
- 新增一个模型，只需要新增一个适配器文件，**流水线代码一行都不用改**（注册见 u7-l1）。

#### 4.2.2 核心流程

契约的执行流程由 `compile-pytorch.py` 主导：

```text
1. load_adapter(--adapter 路径)
       │  （按文件路径把这个 .py 当模块加载进来）
       ▼
2. 检查 adapter 是否有 build_model 和 example_inputs
       │  （缺任意一个 → SystemExit 报错退出）
       ▼
3. model = build_model(--model-path).eval()
       │  （适配器负责把模型实例化出来）
       ▼
4. exported = torch.export.export(model, example_inputs(),
       │                               strict=EXPORT_STRICT)
       │  （用示例输入把模型脱成计算图）
       ▼
5. mlir = export_and_import(exported, output_type="torch")
       │  （把图转成 torch 方言 MLIR 文本）
       ▼
6. 写入 --out 文件
```

契约里「**必须实现**」的是第 2 步那两个函数：

| 契约函数 | 签名 | 返回 | 作用 |
|----------|------|------|------|
| `build_model` | `build_model(model_path) -> torch.nn.Module` | 一个处于 eval 模式的模型 | 实例化模型（可能从权重加载） |
| `example_inputs` | `example_inputs() -> tuple[torch.Tensor, ...]` | 一组示例张量 | 给 `torch.export` 当「模具」 |

还有一个「**可选**」的开关：

| 契约属性 | 类型 | 默认 | 作用 |
|----------|------|------|------|
| `EXPORT_STRICT` | `bool` | `True`（由消费方兜底） | 传给 `torch.export.export(strict=...)` |

> 注意：`EXPORT_STRICT` 的默认值不在适配器里，而在消费方 `compile-pytorch.py` 里用 `getattr(adapter, "EXPORT_STRICT", True)` 兜底——适配器不写就按 `True` 走。

#### 4.2.3 源码精读

先看**消费方如何强制契约**，这是契约的「真身」：

[scripts/compile-pytorch.py:41-46](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L41-L46) —— 检查适配器是否实现了必需函数：

```python
build_model = getattr(adapter, "build_model", None)
example_inputs = getattr(adapter, "example_inputs", None)
if build_model is None or example_inputs is None:
    raise SystemExit(
        f"{args.adapter} must define build_model(model_path) and example_inputs()"
    )
```

这里用 `getattr(..., None)` 安全地「按名字找函数」，找不到就报错退出。这就是「契约」最直接的体现——**没实现这俩名字，流水线根本不会让你往下走**。

[scripts/compile-pytorch.py:48-53](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L48-L53) —— 调用契约函数，导出计算图：

```python
model = build_model(args.model_path).eval()
exported = torch.export.export(
    model,
    tuple(example_inputs()),
    strict=getattr(adapter, "EXPORT_STRICT", True),
)
```

三个要点：

1. `build_model(args.model_path)`：把命令行传进来的 `--model-path` 原样交给适配器。对 matmul 它会被忽略；对 TinyStories 它是 HuggingFace 权重目录。
2. `.eval()`：再保险一次设成推理模式（关掉 dropout 等）。注意 matmul 适配器内部已经 `.eval()` 过一次，这里属于**幂等的二次保险**——`eval()` 调多次没有副作用。
3. `strict=getattr(adapter, "EXPORT_STRICT", True)`：这就是 `EXPORT_STRICT` 的兜底逻辑，默认严格模式。

再看**最小适配器如何满足契约**：

[src/matmul_adapter.py:11-19](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py#L11-L19) —— matmul 适配器实现两个契约函数：

```python
def build_model(_model_path: str | None) -> torch.nn.Module:
    return MatmulModule().eval()


def example_inputs() -> tuple[torch.Tensor, ...]:
    return (
        torch.zeros((16,), dtype=torch.int32),
        torch.zeros((16,), dtype=torch.int32),
    )
```

注意几个细节：

- 形参名写成 `_model_path`（带下划线前缀）：这是 Python 惯用法，表示「这个参数我故意不用」。matmul 没有权重，自然不需要模型路径。
- `build_model` 返回的 `MatmulModule` 来自第 8 行 `MatmulModule = load_matmul_module()`（[src/matmul_adapter.py:5-8](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py#L5-L8)），即 4.1 节讲的动态加载。
- `example_inputs` 返回**两个**长度为 16 的 `int32` 向量，正好对应 `MatmulModule.forward(self, a, b)` 的两个参数。形状/数量/类型必须对齐，否则 `torch.export` 会失败。

最后看消费方如何**动态加载适配器**——这决定了适配器可以放在仓库的任何位置：

[scripts/compile-pytorch.py:14-27](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L14-L27) —— 按文件路径加载适配器模块：

```python
def load_adapter(path: Path) -> ModuleType:
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    ...
    spec.loader.exec_module(module)
    finally:
        try:
            sys.path.remove(str(path.parent))
        ...
```

这里有一个容易被忽略的细节：第 15 行 `sys.path.insert(0, str(path.parent))` 临时把适配器**所在目录**塞进 `sys.path` 头部。这样适配器内部就可以用「平级 import」——比如 matmul 适配器第 5 行直接 `from sim_utils import load_matmul_module`（[src/matmul_adapter.py:5](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py#L5)），能找到 `sim_utils` 是因为 Nix 把 `simDir` 也加进了 `PYTHONPATH`（见 [nix/models.nix:13](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L13)）。`finally` 块再把它移除，避免污染后续加载。

#### 4.2.4 代码实践

**实践目标**：亲手写一个合法适配器，验证「契约 = 两个函数名」。

**操作步骤**（在 `nix develop` 环境里）：

1. 新建文件 `src/add_module.py`（**示例代码**，非项目原有文件）：

   ```python
   import torch

   class AddModule(torch.nn.Module):
       def forward(self, a, b):
           return torch.add(a, b)
   ```

2. 仿照 [src/matmul_adapter.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py) 新建 `src/add_adapter.py`（**示例代码**）：

   ```python
   from __future__ import annotations
   import torch
   from add_module import AddModule   # 与适配器同目录，靠 sys.path.insert 找到

   def build_model(_model_path: str | None) -> torch.nn.Module:
       return AddModule().eval()

   def example_inputs() -> tuple[torch.Tensor, ...]:
       return (
           torch.zeros((4,), dtype=torch.int32),
           torch.zeros((4,), dtype=torch.int32),
       )
   ```

3. 用消费方跑一下（命令参考 README，**待本地验证**完整命令形式）：

   ```bash
   python scripts/compile-pytorch.py --adapter src/add_adapter.py --out /tmp/add.mlir
   ```

**需要观察的现象**：

- 终端打印出 torch 方言 MLIR 文本，里面能看到 `torch.aten.add.Tensor`（或类似加法算子）。
- 如果把 `example_inputs` 改成只返回**一个**张量，再跑一次：应该会报错，因为 `AddModule.forward(self, a, b)` 需要两个输入。

**预期结果**：你得到一个能把 `torch.add` 降级到 torch 方言 MLIR 的最小流水线入口；并直观体会到「**`example_inputs()` 的数量、形状、dtype 必须和 `build_model` 返回模型的 `forward` 形参一一对应**」。

**为什么形状必须一致？** 因为 `torch.export.export(model, tuple(example_inputs()))` 会用这组张量去**实参化** `forward` 并追踪执行。张量的数量对应 `forward` 的位置参数个数，形状/dtype 决定了图里每个值的类型。少一个、形状错一个，要么立刻报错，要么脱出一张「形状不对」的图，把错误推迟到更难排查的下游阶段。

> 待本地验证：本实践未在当前环境实际运行；确切命令与算子名（`add.Tensor` vs `add.Scalar` 等）请以你本地 `compile-pytorch.py` 的输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 matmul 适配器里 `build_model` 的参数名是 `_model_path`，而 TinyStories 适配器里是 `model_path`（不带下划线）？

**答案**：下划线前缀表示「故意不用」。matmul 是无权重的纯算子，不需要模型路径；TinyStories 需要用这个路径去 HuggingFace 加载权重，所以保留了名字（见 4.3 节）。但**两者的函数签名必须一致**，因为消费方 `build_model(args.model_path)` 是按位置传参的。

**练习 2**：如果适配器忘了写 `example_inputs`，错误会在哪一步、以什么形式暴露？

**答案**：在 [scripts/compile-pytorch.py:43-46](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L43-L46) 暴露，形式是 `SystemExit("<adapter> must define build_model(model_path) and example_inputs()")`。也就是契约检查这一关就拦下了，不会等到 `torch.export` 才出错。

**练习 3**：`compile-pytorch.py` 里 `model = build_model(args.model_path).eval()`，而 matmul 适配器的 `build_model` 内部已经 `.eval()` 了。这是 bug 吗？

**答案**：不是。`.eval()` 是幂等的，多次调用等价于一次，没有副作用。消费方再调一次是「防御式编程」——保证无论适配器作者有没有调，最终模型都处于 eval 模式（关掉 dropout/batchnorm 的训练行为），让导出的图稳定。

---

### 4.3 HuggingFace 模型加载与 export 选项

#### 4.3.1 概念说明

上一节的最小适配器 `matmul_adapter.py` 只有一个 `MatmulModule`，没有任何外部文件。但真实项目要降级的是 **TinyStories-1M**——一个约 100 万参数、有权重文件、走 HuggingFace `transformers` 接口的因果语言模型（causal LM）。

这一节要回答：**同一个契约，怎么套到一个真正的 HuggingFace 模型上？** 重点看三件事：

1. 模型权重从哪儿来（`--model-path` 指向什么）。
2. `from_pretrained` 那几个参数为什么那样设。
3. 为什么 TinyStories 要把 `EXPORT_STRICT` 设成 `False`，而 matmul 用默认的 `True`。

#### 4.3.2 核心流程

真实适配器的工作流比 matmul 多了「**加载权重**」这一步：

```text
Nix flake: fetchurl 把 config.json / pytorch_model.bin 钉到固定 revision
       │
       ▼  linkFarm 拼成一个本地目录（snapshot）
       │
适配器 build_model(model_path):
       │  AutoModelForCausalLM.from_pretrained(model_path,
       │       use_cache=False,
       │       attn_implementation="eager",
       │       local_files_only=True)
       ▼  得到一个 nn.Module（带权重）
example_inputs(): 返回 (token_id 张量,) 作为「模具」
       │
       ▼  torch.export.export(..., strict=EXPORT_STRICT=False)
       ▼  export_and_import → torch 方言 MLIR
```

注意整条链路里**没有任何联网**：HuggingFace 的下载发生在 Nix 求值期（`fetchurl`），运行时 `from_pretrained` 用 `local_files_only=True` 只读本地快照。这正呼应了 u1-l3 讲的可复现约束。

#### 4.3.3 源码精读

先看**真实适配器本身**：

[TinyStories/model_adapter.py:12-20](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/TinyStories/model_adapter.py#L12-L20) —— 从 HuggingFace 加载真实模型：

```python
def build_model(model_path: str | None) -> torch.nn.Module:
    if model_path is None:
        raise RuntimeError("TinyStories adapter requires --model-path")
    return AutoModelForCausalLM.from_pretrained(
        model_path,
        use_cache=False,
        attn_implementation="eager",
        local_files_only=True,
    ).eval()
```

对比 matmul 适配器，差异一目了然：

| 维度 | matmul 适配器 | TinyStories 适配器 |
|------|---------------|------------------------|
| `model_path` | 忽略（`_model_path`） | **必需**，缺失即报错 |
| 模型来源 | `MatmulModule()` 直接 new | `AutoModelForCausalLM.from_pretrained(...)` 读权重 |
| 权重 | 无 | 从快照目录加载 |
| 网络 | 无 | `local_files_only=True`，**禁止联网** |

`from_pretrained` 的三个参数都和「**能不能被干净地导出成计算图**」直接相关：

- **`use_cache=False`**：关掉 KV-cache。KV-cache 是为了多步推理时复用历史 K/V，它会让模型带上一堆「上一步状态」的张量，让图变得动态、复杂。我们这里只做**单步前向**导出，不需要它，关掉能让图更简单、更静态。
- **`attn_implementation="eager"`**：用最朴素的 PyTorch eager 注意力实现，而不是 FlashAttention 或 `torch.nn.functional.scaled_dot_product_attention`（SDPA）这类**融合内核**。融合内核是高度优化的黑盒，`torch.export` 很难追踪它的内部计算；而 eager 实现是普通的 matmul + softmax 组合，每个算子都能被工具链看见、降级。
- **`local_files_only=True`**：只从 `model_path` 这个本地目录读文件，不去 HuggingFace Hub 联网。保证构建完全离线、可复现。

接着看**示例输入**：

[TinyStories/model_adapter.py:23-24](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/TinyStories/model_adapter.py#L23-L24) —— 单个 token id 作为输入：

```python
def example_inputs() -> tuple[torch.Tensor, ...]:
    return (torch.zeros((1, 1), dtype=torch.long),)
```

这里只返回**一个**形状为 `(1, 1)` 的 `int64`（`torch.long`）张量——含义是「batch=1、序列长度=1」的一个 token id（值为 0）。这正好是因果语言模型 `forward(input_ids)` 的标准输入。注意它和 matmul 的区别：

- matmul：两个 `int32` 向量 → 给 `torch.matmul`。
- TinyStories：一个 `int64` 的 `[1,1]` → 给语言模型的 `input_ids`。

形状、数量、dtype 全都由**模型 `forward` 的实际签名**决定，这是 4.2 节那条规则的真实体现。

再看**为什么 TinyStories 要关掉 strict**：

[TinyStories/model_adapter.py:9](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/TinyStories/model_adapter.py#L9) —— 关闭严格导出：

```python
EXPORT_STRICT = False
```

`torch.export` 的 `strict=True`（默认）会严格按 `nn.Module` 的 `forward` 签名做图等价校验，遇到动态形状、数据相关控制流（比如某些 embedding 查表、动态 mask）容易失败。真实大模型内部远比 `MatmulModule` 复杂，很难通过严格校验，所以这里显式设 `False` 放宽。而 matmul 没有这一行，于是走消费方兜底的默认 `True`（[scripts/compile-pytorch.py:52](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L52)）——因为它足够简单，严格模式也过得去。

最后看**权重从哪来**，也就是 `--model-path` 指向的那个本地目录：

[flake.nix:200-225](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L200-L225) —— 把 HuggingFace 模型钉成本地快照：

```nix
tinyStories1m = let
  modelId = "roneneldan/TinyStories-1M";
  revision = "77f1b168e219585646439073245fe87e56b3023e";
  fetch = file: hash:
    pkgs.fetchurl {
      url = "https://huggingface.co/${modelId}/resolve/${revision}/${file}";
      inherit hash;
    };
  snapshot = pkgs.linkFarm "tinystories-1m-hf-snapshot" [
    { name = "config.json";     path = fetch "config.json"     "sha256-..."; }
    { name = "pytorch_model.bin"; path = fetch "pytorch_model.bin" "sha256-..."; }
  ];
in {
  inherit snapshot;
  sourceDir = ./TinyStories;
  adapterPy = ./TinyStories/model_adapter.py;
};
```

要点：

- `modelId = "roneneldan/TinyStories-1M"`：模型来自 HuggingFace 上的 `roneneldan/TinyStories-1M`。
- `revision = "77f1..."`：钉死到一个具体 commit，权重永远不变（可复现）。
- `pkgs.fetchurl` + sha256：Nix 在求值期把 `config.json` 和 `pytorch_model.bin` 下载到 store 并校验哈希。
- `pkgs.linkFarm`：把这两个 store 文件拼成一个**长得像 HuggingFace 本地目录**的快照，这个快照就是 `--model-path` 传进去的路径。
- 最终这个 `snapshot` 在 [nix/models.nix:30](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L30) 作为 `--model-path ${tinyStories1m.snapshot}` 传给 `compile-pytorch.py`。

于是整条链是闭环的：Nix 离线拉权重 → 适配器用 `local_files_only=True` 读 → `torch.export` 用 `example_inputs` 脱图 → 转 torch 方言 MLIR。没有一步联网，没有一步用到「不确定版本」的东西。

#### 4.3.4 代码实践

**实践目标**：理解 `from_pretrained` 的三个参数对「可导出性」的影响。

**操作步骤**（源码阅读型实践）：

1. 打开 [TinyStories/model_adapter.py:12-20](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/TinyStories/model_adapter.py#L12-L20)。
2. 逐个回答：如果删掉 `use_cache=False`、删掉 `attn_implementation="eager"`、把 `local_files_only=True` 改成 `False`，分别会**在哪一环**出问题？
3. 打开 [flake.nix:200-225](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L200-L225)，确认快照只包含 `config.json` 和 `pytorch_model.bin` 两个文件。

**需要观察的现象 / 预期结论**：

| 改动 | 预期后果 |
|------|----------|
| 去掉 `use_cache=False` | 模型 forward 会带 KV-cache 状态张量，导出的图出现额外的 past/future 依赖，形状变动态 |
| 去掉 `attn_implementation="eager"` | 默认可能走 SDPA 融合内核，`torch.export` 追踪不到内部算子，下游 torch-MLIR 找不到对应 lowering |
| `local_files_only=True`→`False` | 构建时会试图联网访问 HuggingFace，破坏「完全离线/可复现」，且在无网 CI 里直接失败 |

> 待本地验证：上述「预期后果」是依据 PyTorch/transformers 通用行为给出的推理，具体报错信息请以本地 `compile-pytorch.py` 实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `example_inputs()` 对 TinyStories 返回的是 `torch.long`（int64）而不是 `int32`？

**答案**：因为语言模型的输入是 **token id**（词表里的整数下标），HuggingFace 模型的 `forward` 约定 `input_ids` 是 `int64`/`long`。`example_inputs` 的 dtype 必须匹配 `forward` 的实际期望类型，否则 `torch.export` 会报类型不匹配。对比 matmul 用 `int32` 是因为它的算子（`torch.matmul`）在 int32 上就能算，且仿真黄金参考（u4-l1）选了 int32 方便比对。

**练习 2**：`EXPORT_STRICT = False` 对 matmul 为什么不必要？

**答案**：`MatmulModule` 没有动态形状、没有数据相关控制流，是最「静态」的模型，完全经得起 `strict=True` 的等价校验。所以 matmul 适配器干脆不写这个属性，让消费方兜底的默认 `True` 生效。换言之：**只有当模型复杂到通不过严格校验时，才需要显式放宽**。

**练习 3**：`flake.nix` 里为什么用 `pkgs.linkFarm` 而不是直接用 `fetchurl` 的单个文件作为 `--model-path`？

**答案**：`from_pretrained(model_path)` 需要一个**目录**，里面同时有 `config.json` 和 `pytorch_model.bin` 才能加载。单个 `fetchurl` 只是一个文件，不是目录。`linkFarm` 把多个 store 文件拼成一个「看起来像 HuggingFace 本地目录」的结构，正好满足 `from_pretrained` 对目录布局的要求。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合任务：

**任务**：假设你要把一个新模型接入流水线，它是一个只有一层线性层的 `nn.Module`：`y = Wx + b`（`W` 是 `[4,4]`、`b` 是 `[4]`，都随机初始化、`requires_grad=False`）。请完成：

1. **写模型本身**（`linear_module.py`，示例代码）：定义一个继承 `nn.Module` 的 `LinearModule`，`forward(self, x)` 返回 `self.lin(x)`，在 `__init__` 里用 `nn.Linear(4, 4, bias=True)` 创建 `self.lin`。

2. **写适配器**（`linear_adapter.py`，示例代码）：实现
   - `build_model(model_path)`：实例化 `LinearModule().eval()`（这个模型权重内置，`model_path` 可忽略）。
   - `example_inputs()`：返回**一个**形状 `(4,)` 的张量，dtype 用 `torch.float32`。

3. **回答两个判断题**（用本讲学到的契约知识）：
   - 这个适配器该不该设 `EXPORT_STRICT = False`？为什么？
   - 如果把 `example_inputs` 的形状从 `(4,)` 改成 `(8,)`，会在哪一步出错？

**参考答案要点**：

1. 模型示例：

   ```python
   import torch
   class LinearModule(torch.nn.Module):
       def __init__(self):
           super().__init__()
           self.lin = torch.nn.Linear(4, 4, bias=True)
       def forward(self, x):
           return self.lin(x)
   ```

2. 适配器示例：

   ```python
   from __future__ import annotations
   import torch
   from linear_module import LinearModule

   def build_model(_model_path: str | None) -> torch.nn.Module:
       return LinearModule().eval()

   def example_inputs() -> tuple[torch.Tensor, ...]:
       return (torch.zeros((4,), dtype=torch.float32),)
   ```

   > 提示：`nn.Linear` 的 `forward` 要求最后一维等于 `in_features=4`，所以示例输入最后一维必须是 4，dtype 与权重一致用 `float32`。

3. 判断题：
   - **不必要**设 `EXPORT_STRICT = False`。单层 `nn.Linear` 是最静态的算子之一，没有动态形状、没有数据相关控制流，能通过 `strict=True`（消费方默认值）。和 matmul 同理。
   - 改成 `(8,)` 会在 **`torch.export.export`** 这一步（[compile-pytorch.py:49-53](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L49-L53)）报错——因为 `nn.Linear(4,4)` 要求输入最后一维是 4，传入 8 维不匹配，矩阵乘形状对不上。

> 待本地验证：以上代码为示例，未在当前环境运行；请在你本地 `nix develop` 环境里用 `compile-pytorch.py --adapter src/linear_adapter.py --out /tmp/linear.mlir` 实际验证，预期看到包含 `torch.aten.linear` 或 `matmul`+`add` 算子的 torch 方言 MLIR。

## 6. 本讲小结

- LLM2FPGA 把「**模型本身**」（如 `src/matmul.py`）和「**适配器**」（如 `src/matmul_adapter.py`）分层：模型该怎么写还怎么写，流水线只认适配器那几个约好名字的函数。
- 适配器的**契约**是两个必需函数 `build_model(model_path)` 与 `example_inputs()`，加一个可选的 `EXPORT_STRICT`；契约的权威定义和强制执行在**消费方** [scripts/compile-pytorch.py:41-46](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L41-L46)。
- `example_inputs()` 的**数量、形状、dtype 必须与 `build_model` 返回模型的 `forward` 形参一一对应**——因为它是 `torch.export` 把模型脱成计算图的「模具」。
- 最小核 `MatmulModule` 无权重、纯算子，是整条降级链的冒烟测试；`sim_utils.load_matmul_module()` 用 `importlib` 按路径加载，并支持 `MATMUL_PY` 环境变量覆盖以保证可复现。
- 真实适配器 [TinyStories/model_adapter.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/TinyStories/model_adapter.py) 用 `from_pretrained(use_cache=False, attn_implementation="eager", local_files_only=True)` 让大模型可被干净导出，并用 `EXPORT_STRICT=False` 放宽严格校验。
- HuggingFace 权重由 [flake.nix:200-225](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L200-L225) 在求值期离线 `fetchurl` + `linkFarm` 钉成本地快照，整条链完全离线、可复现。

## 7. 下一步学习建议

本讲只到「把模型脱成 torch 方言 MLIR」的门口。接下来：

- **u2-l2（compile-pytorch.py 与 torch.export）**：逐行精读消费方 [scripts/compile-pytorch.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py)，看清 `load_adapter` 的 sys.path 技巧、`torch.export.export(strict=...)` 的细节、以及 `torch_mlir.fx.export_and_import` 如何把图转成 torch 方言 MLIR 文本。本讲的适配器契约将在那里被真正「消费」。
- **u2-l3（torch-MLIR 前端降级）**：进入降级链的下一站，看 torch 方言如何经 torch-MLIR 两条 backend pipeline 降到 Linalg-on-Tensors。
- **u7-l1（注册一个新模型）**：等你写完自己的适配器，去 [nix/models.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix) 看如何用 `registerModel` 把它注册进流水线、复用整条降级链——那是本讲「写适配器」的最终归宿。

**建议阅读的源码顺序**：先重读 [scripts/compile-pytorch.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py)（消费方），再对照 [src/matmul_adapter.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py) 与 [TinyStories/model_adapter.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/TinyStories/model_adapter.py)（两个实现方），最后看 [flake.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L200-L225) 的权重快照，形成「契约 → 实现 → 权重供给」的完整闭环认知。
