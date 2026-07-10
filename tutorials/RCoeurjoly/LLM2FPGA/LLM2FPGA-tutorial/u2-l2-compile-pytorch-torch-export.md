# compile-pytorch.py 与 torch.export

## 1. 本讲目标

本讲是 PyTorch 前端的「发动机」：我们逐行拆解整个流水线最上游的入口脚本 `scripts/compile-pytorch.py`。它只有大约 60 行，却完成了三件事——**把任意一个 PyTorch 模型，冻结成一张静态计算图，再翻译成 torch 方言的 MLIR 文本**。

学完本讲你应当能够：

1. 说清 `compile-pytorch.py` 为什么用 `importlib`「按文件路径动态加载适配器」，以及 `sys.path` 在这里的微妙作用。
2. 解释 `torch.export.export(..., strict=...)` 在干什么，`example_inputs()` 为什么是「模具」，以及 `EXPORT_STRICT` 为什么 TinyStories 要设成 `False` 而 matmul 用默认 `True`。
3. 看懂 `torch_mlir.fx.export_and_import(exported, output_type="torch")` 产出的 torch 方言 MLIR 长什么样，以及为什么本脚本故意停在 torch 方言、不继续往下降级。
4. 把这三个步骤串成一条「一次 matmul 调用」的完整数据流，并实跑脚本、截取前 40 行输出。

本讲承接 [u2-l1 模型与适配器契约](u2-l1-models-and-adapters.md)：上一讲确立了「适配器必须实现 `build_model` 与 `example_inputs`」的契约，本讲就去看**契约的验收方** `compile-pytorch.py` 是怎么消费这两个函数的。

---

## 2. 前置知识

### 2.1 从「带权重的 Python 对象」到「静态计算图」

一个 `torch.nn.Module`（比如 [src/matmul.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul.py) 里的 `MatmulModule`）本质是「一段会真正执行计算的 Python 代码」。它每次 `forward` 都在**解释执行**：你能写 `if`、`for`、调任意库函数。

但硬件不能解释执行 Python。要变成 FPGA，必须先把模型脱成一张**静态计算图**——节点是算子（如 matmul、add），边是张量，所有动态控制流都被展开或固化。`torch.export` 就是 PyTorch 2.x 用来做这件事的官方 API。

### 2.2 什么叫「用示例输入当模具」

`torch.export` 不是凭空知道输入张量形状的。你必须喂它一组**示例输入**（example inputs）。它拿着这组输入去跑一遍 `forward`，但**只记录算子序列**，并把示例输入的**形状和 dtype 烧进图里**当成常量。所以示例输入就是「模具」：它的形状决定了输出图的形状。这也解释了 u2-l1 反复强调的铁律——`example_inputs()` 的形状/dtype/数量必须和 `forward` 形参一一对应。

### 2.3 MLIR、dialect、lowering 一句话回顾

承接 u2-l1：**lowering（降级）** 是把高层表示一步步翻译成更低层、更接近硬件的表示；每一步的「表示」叫一个 **dialect（方言）**。本讲产出的就是**最顶层的 torch 方言**（算子写成 `torch.aten.matmul` 这种），后续 [u2-l3](u2-l3-torch-mlir-frontend-lowering.md) 会把它降到 Linalg。

### 2.4 importlib 按路径加载模块

Python 默认通过包名（`import foo`）找模块，依赖 `sys.path` 和包结构。但这里适配器是用户传入的**任意文件路径**（`--adapter src/matmul_adapter.py`），所以要用标准库 `importlib.util.spec_from_file_location` 按绝对路径加载，绕开包发现机制。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲解读的部分 |
|------|------|----------------|
| [scripts/compile-pytorch.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py) | 流水线最上游入口脚本，本讲的绝对主角 | 全文（约 60 行） |
| [src/matmul_adapter.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py) | matmul 的适配器（最小核） | 与脚本的契约配合 |
| [TinyStories/model_adapter.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/TinyStories/model_adapter.py) | TinyStories-1M 的适配器（真实大模型） | `EXPORT_STRICT=False` 的来源 |
| [src/matmul.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul.py) | matmul 模型本体 | 被导出的 forward |
| [sim/sim_utils.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/sim_utils.py) | `load_matmul_module`，按路径加载 matmul 本体 | 适配器内部如何拿到 `MatmulModule` |
| [nix/models.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix) | 把脚本包成 Nix 派生、拼出真实命令行 | matmul 与 TinyStories 的 `torchInputCommand` |

一句话定位：`compile-pytorch.py` 是「适配器 → torch 方言 MLIR」的单站式转换器；它本身不做任何降级，降级是下一站 [u2-l3](u2-l3-torch-mlir-frontend-lowering.md) 的事。

---

## 4. 核心概念与源码讲解

### 4.1 动态加载适配器模块

#### 4.1.1 概念说明

`compile-pytorch.py` 通过命令行参数 `--adapter` 接收一个 `.py` 文件路径。它不预先知道是 matmul 还是 TinyStories——任何实现了契约（`build_model` + `example_inputs`）的文件都能被它消费。这种「运行时才知道加载谁」的需求，必须用**按路径动态加载**。

这里有个容易踩的坑：`importlib` 按路径加载某个文件时，只加载那一个文件；但该文件内部如果又 `import` 了兄弟模块（例如 [src/matmul_adapter.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py) 里 `from sim_utils import load_matmul_module`），Python 的 import 机制只会去 `sys.path` 里找——而适配器所在的目录默认并不在 `sys.path` 上，于是兄弟 import 会失败。脚本的 `sys.path.insert` 就是为解决这一点。

#### 4.1.2 核心流程

加载一个适配器的步骤如下（伪代码）：

```
输入: path = 用户传入的适配器文件路径（如 src/matmul_adapter.py）
1. 把 path 的父目录临时插到 sys.path 最前面  # 让兄弟 import 能解析
2. 用 importlib 按 path 造一个模块规格 spec（模块名取文件名 stem）
3. 由 spec 创建空模块对象
4. 执行 spec.loader.exec_module(module)  # 真正跑一遍这个 .py，填充 module
5. finally: 把刚才插入的父目录从 sys.path 移除  # 保持 sys.path 干净
6. 返回 module（此时它已带有 build_model / example_inputs 属性）
```

注意第 5 步的 `finally`：无论加载成功还是抛异常，都要把 `sys.path` 改动回滚，避免污染后续其他模型的加载——这是脚本级的「作用域卫生」。

#### 4.1.3 源码精读

加载逻辑全部在 `load_adapter` 函数里：

[scripts/compile-pytorch.py:14-27](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L14-L27) —— 把适配器文件按路径动态导入，并临时维护 `sys.path`：

```python
def load_adapter(path: Path) -> ModuleType:
    sys.path.insert(0, str(path.parent))                         # ① 临时让兄弟 import 可见
    spec = importlib.util.spec_from_file_location(path.stem, path)  # ② 模块名=文件名，位置=path
    if spec is None or spec.loader is None:
        raise SystemExit(f"unable to load adapter module from {path}")
    module = importlib.util.module_from_spec(spec)               # ③ 创建空模块对象
    try:
        spec.loader.exec_module(module)                          # ④ 真正执行该 .py，填充属性
    finally:
        try:
            sys.path.remove(str(path.parent))                    # ⑤ 回滚 sys.path
        except ValueError:
            pass
    return module
```

几个要点：

- [scripts/compile-pytorch.py:15](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L15) 的 `sys.path.insert(0, ...)`：`0` 表示插到最前，保证适配器自带的同名模块优先于全局环境里的同名包被选中。
- [scripts/compile-pytorch.py:16](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L16) 的 `path.stem`：对 `matmul_adapter.py` 就是 `matmul_adapter`，它成为这个模块在 `sys.modules` 里的注册名。
- [scripts/compile-pytorch.py:21](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L21) 的 `exec_module` 才是真正「执行」适配器文件的语句——此前模块对象是空的。

再看一个真实的兄弟 import 例子，体会为什么需要 `sys.path.insert`：

[src/matmul_adapter.py:5-8](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py#L5-L8) —— matmul 适配器在文件顶部 `import` 了同仓库的 `sim_utils`：

```python
from sim_utils import load_matmul_module

MatmulModule = load_matmul_module()
```

`sim_utils` 来自 [sim/sim_utils.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/sim_utils.py)，它本身又用同样的「按路径加载」手法拿到 `MatmulModule`：

[sim/sim_utils.py:8-16](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/sim_utils.py#L8-L16) —— 受环境变量 `MATMUL_PY` 控制，默认指向仓库内的 `src/matmul.py`：

```python
def load_matmul_module():
    default_matmul_path = Path(__file__).parent.parent / "src" / "matmul.py"
    matmul_path = Path(os.environ.get("MATMUL_PY", str(default_matmul_path)))
    ...
    spec.loader.exec_module(module)
    return module.MatmulModule
```

> 注意：matmul 适配器实际运行时能找到 `sim_utils`，靠的是 [nix/models.nix:11-17](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L11-L17) 里把 `src/`、`sim/` 都加进了 `PYTHONPATH`。脚本内的 `sys.path.insert` 是更通用的「兜底」，保证即使没配 `PYTHONPATH`、只要适配器和它依赖的模块在同一目录也能加载。

#### 4.1.4 代码实践

**目标**：验证 `sys.path.insert` 与 `finally` 回滚的必要性。

**步骤（源码阅读型）**：

1. 读 [scripts/compile-pytorch.py:14-27](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L14-L27)。
2. 假设把第 15 行 `sys.path.insert(0, str(path.parent))` 注释掉：写一段不超过 80 字的分析，说明在「`PYTHONPATH` 没有预先配好」的情况下，加载一个含 `from sibling import x` 的适配器会发生什么。
3. 思考：为什么回滚用的是 `sys.path.remove(...)` 包在 `try/except ValueError` 里，而不是 `sys.path.pop(0)`？

**预期结果**：你能说出——删掉 insert 后兄弟 import 会因 `ModuleNotFoundError` 失败；用 `pop(0)` 不安全，因为 `exec_module` 期间如果适配器自己也改了 `sys.path`，「栈顶」可能已经不是我们插入的那一项，而 `remove(具体值)` 按值删除才精准。

#### 4.1.5 小练习与答案

**练习 1**：`spec_from_file_location(path.stem, path)` 的第一个参数 `path.stem` 影响什么？

**参考答案**：它是被加载模块的名字（即 `module.__name__`，也用于注册进 `sys.modules`）。对 `matmul_adapter.py`，stem 是 `matmul_adapter`。它不影响文件从哪里读（那由第二个参数 `path` 决定），只影响模块的「身份」。

**练习 2**：如果用户传的 `--adapter` 指向一个不存在的文件，`load_adapter` 会怎样？

**参考答案**：`spec_from_file_location` 对不存在的路径仍可能返回非 None 的 spec，但在 [scripts/compile-pytorch.py:21](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L21) 的 `exec_module` 时会抛 `FileNotFoundError`/`ImportError`；脚本没有捕获它，所以会带着原始回溯退出（注：具体异常类型「待本地验证」）。

---

### 4.2 torch.export.export 导出图

#### 4.2.1 概念说明

加载完适配器，下一步是把模型变成静态图。核心是 [torch.export.export](https://pytorch.org/docs/stable/export.html)：它接收「模型 + 一组示例输入」，**用示例输入驱动一次符号化追踪**，产出一个 `ExportedProgram`——这是一个冻结的、不再依赖原始 Python 代码的计算图对象。

这里有两个工程细节必须讲清：

1. **`.eval()` 的意义**：`build_model(...)` 返回的模型在导出前被切到 eval 模式（见 [scripts/compile-pytorch.py:48](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L48) 的 `.eval()`）。这会关闭 dropout、把 BatchNorm 等切到推理行为，保证导出的图就是「推理时」的行为，而不是「训练时」。
2. **`strict` 参数与 `EXPORT_STRICT`**：`torch.export.export` 有个 `strict` 开关。
   - `strict=True`（默认）：用 TorchDynamo 做符号追踪，会把高层算子分解成更底层的 ATen 子集，并尝试把 Python 控制流固化或加守卫；它更精确，但对 Dynamo 不认识的 Python 写法会报错。
   - `strict=False`：走非 Dynamo 的「eager 记录」追踪，更宽容——实际跑一遍 `forward`、把遇到的算子按原样记下来，适合那些 Dynamo 追不动的复杂模型。

脚本的精巧之处在于：`strict` 的值**不是写死的**，而是问适配器借的——`getattr(adapter, "EXPORT_STRICT", True)`，默认 `True`，但适配器可以设 `EXPORT_STRICT = False` 来改用宽容路径。

#### 4.2.2 核心流程

```python
model   = build_model(model_path).eval()        # ① 建模 + 切推理模式
exported = torch.export.export(
    model,
    tuple(example_inputs()),                     # ② 示例输入作为「模具」
    strict=getattr(adapter, "EXPORT_STRICT", True),  # ③ strict 由适配器决定
)
# exported 是一个 ExportedProgram：冻结的静态图（还不是 MLIR）
```

数据形态变化：

\[
\text{torch.nn.Module} \;\xrightarrow{\text{torch.export}}\; \text{ExportedProgram（静态图，含 ATen 算子）}
\]

注意：到这一步**还没有任何 MLIR**，`ExportedProgram` 仍是 PyTorch 自己的图对象。变成 MLIR 是 4.3 的事。

#### 4.2.3 源码精读

先看脚本如何「问适配器要契约」并做硬校验：

[scripts/compile-pytorch.py:41-46](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L41-L46) —— 取出契约函数，缺一不可：

```python
build_model = getattr(adapter, "build_model", None)
example_inputs = getattr(adapter, "example_inputs", None)
if build_model is None or example_inputs is None:
    raise SystemExit(
        f"{args.adapter} must define build_model(model_path) and example_inputs()"
    )
```

这正是 u2-l1 强调的「契约的权威定义与强制执行在消费方」——`compile-pytorch.py` 才是验收人。

接着是导出本体：

[scripts/compile-pytorch.py:48-53](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L48-L53) —— 建模、切 eval、用示例输入导出：

```python
model = build_model(args.model_path).eval()
exported = torch.export.export(
    model,
    tuple(example_inputs()),
    strict=getattr(adapter, "EXPORT_STRICT", True),
)
```

为什么 TinyStories 要关掉 strict？看它的适配器：

[TinyStories/model_adapter.py:9](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/TinyStories/model_adapter.py#L9) —— 显式设为非严格：

```python
EXPORT_STRICT = False
```

原因：TinyStories-1M 是 HuggingFace 的 GPT 类因果语言模型，内部 Python 控制流和算子组合复杂，Dynamo 严格追踪（`strict=True`）容易在某些算子上失败；`EXPORT_STRICT = False` 让导出走更宽容的记录路径。而 [src/matmul_adapter.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py) 没有定义 `EXPORT_STRICT`，于是 `getattr(..., True)` 取默认 `True`——因为 matmul 太简单，严格追踪毫无压力。

再看两个适配器的 `example_inputs` 如何充当「模具」，对比最小核与真实模型：

[src/matmul_adapter.py:15-19](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py#L15-L19) —— matmul 喂两个 int32 长度 16 的向量：

```python
def example_inputs() -> tuple[torch.Tensor, ...]:
    return (
        torch.zeros((16,), dtype=torch.int32),
        torch.zeros((16,), dtype=torch.int32),
    )
```

[TinyStories/model_adapter.py:23-24](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/TinyStories/model_adapter.py#L23-L24) —— 语言模型喂一个 batch=1、seq=1 的 token id：

```python
def example_inputs() -> tuple[torch.Tensor, ...]:
    return (torch.zeros((1, 1), dtype=torch.long),)
```

这两个返回值的**形状与 dtype 会原样烧进导出图**——这就是「模具」的实际含义。它们也分别匹配各自 `forward` 的入参：matmul 的 `forward(self, a, b)` 有两个张量入参，语言模型的 `forward(input_ids=...)` 期望一个 `[batch, seq]` 的 long 张量。

#### 4.2.4 代码实践

**目标**：直观感受「示例输入 = 模具」。

**步骤（源码阅读 + 推理型）**：

1. 假设把 [src/matmul_adapter.py:15-19](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py#L15-L19) 改成只返回一个张量（删掉第二个），即 `example_inputs` 与 `forward(self, a, b)` 的双入参不匹配。
2. 推理：`torch.export.export(model, tuple(example_inputs()), ...)` 会发生什么？
3. 再假设把 `dtype=torch.int32` 改成 `dtype=torch.float32`：模型的 `torch.matmul` 输入类型随之变化，导出图里张量类型会变成什么？

**预期结果**：
- 情况 1 会因「示例输入数量与 forward 形参不一致」在 `torch.export` 阶段报错（参数个数对不上）。
- 情况 2 会导致导出图里张量类型变成 `!torch.tensor<[16]xf32>`（float32），进而影响后续整个降级链的类型推导。
- 具体异常文本「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么导出前要 `.eval()`？能不能省略？

**参考答案**：`.eval()` 把模块切到推理模式，关闭 dropout 等训练期随机行为，并让 BatchNorm/LayerNorm 等用推理统计量。省略会导致导出的图可能含训练期算子（如 dropout），这与「部署推理」语义不符，也无法稳定复现。

**练习 2**：`strict=True` 和 `strict=False` 各自适合什么场景？

**参考答案**：`strict=True`（Dynamo 追踪）适合结构简单、Dynamo 能完整分析的模型，产出更底层、更规整的算子图；`strict=False`（eager 记录）适合 Dynamo 追不动的复杂模型（如很多 HuggingFace 大模型），更宽容但保留的算子可能更「原始」。本项目 matmul 用 `True`、TinyStories-1M 用 `False` 正是这个权衡。

---

### 4.3 torch_mlir.fx.export_and_import 转 MLIR

#### 4.3.1 概念说明

`ExportedProgram` 还是 PyTorch 的私人物品，硬件工具链看不懂。最后一步用 **torch-MLIR** 把它翻译成 **MLIR 文本**，写进文件。

关键在 `output_type="torch"` 这个选择。torch-MLIR 支持几种「降到哪个方言为止」的输出类型，自上而下越来越底层：

| output_type | 停在哪个方言 | 算子长这样 |
|-------------|--------------|-----------|
| `"torch"` | torch 方言（最高层） | `torch.aten.matmul` |
| `"tosa"` | TOSA 方言 | `tosa.matmul` |
| `"linalg-on-tensors"` | Linalg 方言 | `linalg.matmul` |

本脚本**故意只选 `"torch"`**——即只翻译、不降级。为什么不下沉到 Linalg？因为降级是下一站 [scripts/pipeline/torch_to_linalg.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/torch_to_linalg.sh)（详见 [u2-l3](u2-l3-torch-mlir-frontend-lowering.md)）用 `torch-mlir-opt` 跑 backend pipeline 干的活。把「捕获成 torch 方言」和「降级到 Linalg」拆成两个独立的、可被 Nix 缓存的派生（见 [nix/pipeline.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix) 的 `mkTorchInput` 与 `mkLinalgDerivation`），改其中一步不必重跑另一步。

#### 4.3.2 核心流程

```python
module     = export_and_import(exported, output_type="torch")  # ① ExportedProgram → torch 方言 MLIR 模块
mlir_text  = str(module)                                       # ② ModuleOp 序列化成文本 IR
args.out.write_text(mlir_text, encoding="utf-8")               # ③ 写到 --out（派生产物 *-torch.mlir）
print(mlir_text)                                               # ④ 同时打印到 stdout
```

数据形态变化：

\[
\text{ExportedProgram} \;\xrightarrow{\text{export\_and\_import}(\text{output\_type}=\text{"torch"})}\; \text{MLIR ModuleOp（torch 方言文本）}
\]

#### 4.3.3 源码精读

脚本的最后四行就是全部：

[scripts/compile-pytorch.py:54-57](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L54-L57) —— 翻译成 torch 方言 MLIR 并落盘：

```python
module = export_and_import(exported, output_type="torch")
mlir_text = str(module)
args.out.write_text(mlir_text, encoding="utf-8")
print(mlir_text)
```

而 `export_and_import` 来自脚本顶部的导入：

[scripts/compile-pytorch.py:10](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L10)：

```python
from torch_mlir.fx import export_and_import
```

它产出的 MLIR 文本大致结构如下（以 matmul 为例的**示意**，精确文本「待本地验证」）：

```mlir
module {                                  # 顶层模块（torch-mlir 通常命名为 @main 或 @__torch__.<类名>）
  func.func @main(%arg0 : !torch.tensor<[16]xi32>, %arg1 : !torch.tensor<[16]xi32>)
      -> !torch.tensor<...> {
    %0 = torch.aten.matmul %arg0, %arg1 : !torch.tensor<[16]xi32>, !torch.tensor<[16]xi32> -> !torch.tensor<...>
    return %0 : !torch.tensor<...>
  }
}
```

几个特征要记住，下一讲会反复用到：

- 顶层是一个 `module`，里面是一个入口 `func.func`。
- 张量类型写成 `!torch.tensor<[16]xi32>`——这个 `[16]` 和 `i32` 正是从 4.2 的「模具」烧进来的。
- 算子写成 `torch.aten.matmul` 这种 **torch 方言** 形式，这正是 `output_type="torch"` 的产物，也是 [u2-l3](u2-l3-torch-mlir-frontend-lowering.md) 要继续往下降级的起点。

产物的去向：`args.out` 在 Nix 里被绑成派生输出 `${name}-torch.mlir`（见 [nix/pipeline.nix:20](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L20) 的 `runCommand "${name}-torch.mlir"`），也就是 [nix/models.nix:14-16](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L14-L16) 里 `python ${compilePyTorch} --adapter ... --out "$out"` 的 `$out`。

#### 4.3.4 代码实践

**目标**：确认产物停留在 torch 方言、且形状来自模具。

**步骤（源码阅读型）**：

1. 读 [scripts/compile-pytorch.py:54](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L54)，确认 `output_type="torch"`。
2. 读 [nix/pipeline.nix:30-34](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L30-L34) 的 `mkLinalgDerivation`，确认下一站才用 `torch_to_linalg.sh` 把 torch 方言降级。
3. 回答：如果本脚本把 `output_type` 改成 `"linalg-on-tensors"`，`nix/pipeline.nix` 里哪一站会变得多余/冲突？

**预期结果**：你能指出——那样 `compile-pytorch.py` 自己就降到了 Linalg，而 [nix/pipeline.nix:30-34](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L30-L34) 的 `mkLinalgDerivation`（跑 `torch_to_linalg.sh`）就会对一个已经是 Linalg 的输入再跑一次 torch→linalg 降级，语义冲突。这正是为什么本脚本必须停在 `"torch"`。

#### 4.3.5 小练习与答案

**练习 1**：`str(module)` 里的 `module` 是什么类型？

**参考答案**：它是一个 MLIR `ModuleOp`（Python 对象，由 torch-MLIR 构造）。`str()` 调用 MLIR 的文本序列化，把它打印成可读的 MLIR 文本 IR。它不是字符串，但能被 `str()` 转成字符串。

**练习 2**：为什么脚本既 `write_text` 又 `print` 同样的内容？

**参考答案**：`write_text` 是把产物落盘成 Nix 派生输出文件（必需，下游派生靠这个文件）；`print` 到 stdout 是为了人在直接运行脚本（如调试）时能立即在终端看到 MLIR。在 Nix 流水线里，stdout 常被重定向到 `/dev/null`（见 [nix/models.nix:16](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L16) 的 `>/dev/null`），所以 print 不影响产物。

---

## 5. 综合实践

**贯穿任务：把 `compile-pytorch.py` 真正跑在 matmul 适配器上，截取 torch 方言 MLIR 的前 40 行。**

这个任务同时调动本讲三个模块：动态加载 matmul 适配器（4.1）、用 `torch.export` 导出 `MatmulModule`（4.2）、用 `export_and_import` 翻译成 torch 方言（4.3）。

### 操作步骤

**第 1 步：读懂 Nix 里 matmul 的真实命令。** 读 [nix/models.nix:11-17](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L11-L17)：

```nix
torchInputCommand = ''
  export MATMUL_PY="${matmulPy}"
  export PYTHONPATH="${matmulSrcDir}:${simDir}:${torchMlirPythonPath}:''${PYTHONPATH:-}"
  python ${compilePyTorch} \
    --adapter ${matmulAdapterPy} \
    --out "$out" >/dev/null
'';
```

可以看到它做了三件事：设 `MATMUL_PY`（让 [sim/sim_utils.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/sim/sim_utils.py) 知道去哪读 matmul 本体）、设 `PYTHONPATH`（让 `torch`、`torch_mlir`、`sim_utils` 都能被 import）、再调用脚本。

**第 2 步：进入开发 shell。** 执行 `nix develop`（见 [flake.nix:761-788](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L761-L788) 的 devShell，含 `torchMlir`、`pythonWithTorch`）。

**第 3 步：在仓库根目录，复现上面的命令并截取前 40 行。** 在 devShell 里，torch_mlir 的 site-packages 路径需要自己拼（devShell 没有把它直接加进 `PYTHONPATH`）。一个等价于 Nix 命令的手动复现思路如下（**具体 store 路径与精确 IR 文本「待本地验证」**）：

```bash
# 设 matmul 本体路径
export MATMUL_PY="$PWD/src/matmul.py"

# 让 torch_mlir 可被 import：从 torchMlir 派生里找 site-packages。
# 注意：python 用的是 pkgsLlvm21.python311（3.11），所以是 lib/python3.11/site-packages。
TM=<填入你机器上 torchMlir 的 /nix/store 前缀>
SP="$TM/lib/python3.11/site-packages"
export PYTHONPATH="$PWD/src:$PWD/sim:$SP:$SP/torch_mlir:${PYTHONPATH:-}"

# 运行脚本，取前 40 行（去掉 >/dev/null，这样能看到 stdout 的 MLIR）
python scripts/compile-pytorch.py \
  --adapter src/matmul_adapter.py \
  --out /tmp/matmul-torch.mlir | head -n 40
```

> 如果手动拼 `TM` 困难，可以退而求其次：直接构建一条经过 torch 阶段的已暴露派生（如 `nix build .#matmul-selftest-bitstream`，见 [flake.nix:792-798](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L792-L798)），再用 `nix path-info` / `find /nix/store -name '*-torch.mlir'` 定位 torch 阶段产物后用 `head -n 40` 查看——这条路径更慢但完全可复现。注意：`.#matmul.pipeline.torch` 这类中间阶段**并未**作为独立 `packages` 暴露，所以不能直接 `nix build .#matmul.pipeline.torch`。

### 需要观察的现象

在前 40 行里，请圈出并记录：

1. **顶层 `module` 声明**：是 `module {` 还是 `module @main {` 或 `module @__torch__.MatmulModule {`？（以本地输出为准——torch-mlir 的命名随版本变化。）
2. **入口 `func.func`**：它的参数类型应当是 `!torch.tensor<[16]xi32>` 两个，正好对应 [src/matmul_adapter.py:15-19](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py#L15-L19) 喂的模具。
3. **`torch.aten.matmul` 算子**：这是 [src/matmul.py:5](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul.py#L5) 的 `torch.matmul(a, b)` 翻译到 torch 方言后的样子。
4. **没有 Linalg、没有 `linalg.*`**：确认 `output_type="torch"` 确实停在了 torch 方言。

### 预期结果

- 你会看到一段以 `module` 开头的 MLIR 文本；
- 含一个 `func.func` 入口，入参类型与模具一致；
- 含至少一个 `torch.aten.matmul`；
- 全文**不**含 `linalg.`、`scf.`、`cf.` 等更低层方言的算子。
- 精确 IR 文本（尤其是模块命名、返回张量的确切形状/类型）「待本地验证」，但上述结构特征应当稳定出现。

> 反思题：把输出与 [TinyStories/model_adapter.py:24](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/TinyStories/model_adapter.py#L24) 的模具 `(1,1) long` 对照——若改跑 TinyStories，入参类型应变成 `!torch.tensor<[1,1]xi64>`，算子也会多得多（整个 transformer）。这说明同一个脚本、同一个流程，**换适配器就能换模型**，这正是「模型/适配器分离」的价值。

---

## 6. 本讲小结

- `compile-pytorch.py` 是流水线最上游入口：**适配器 → torch 方言 MLIR**，它本身不做任何降级。
- 它用 `importlib` 按文件路径**动态加载适配器**，并用 `sys.path.insert` + `finally` 回滚来保证兄弟 import 可解析且不污染全局路径（[4.1](#41-动态加载适配器模块)）。
- 它用 `torch.export.export(model, tuple(example_inputs()), strict=...)` 把模型冻结成静态图；`example_inputs()` 是「模具」，形状/dtype 被烧进图里；`strict` 由适配器的 `EXPORT_STRICT` 决定，matmul 用默认 `True`、TinyStories 用 `False`（[4.2](#42-torchexportexport-导出图)）。
- 它用 `export_and_import(exported, output_type="torch")` 翻译成 **torch 方言** MLIR，故意停在最高层，把降级留给下一站，让两步各自成为可缓存的 Nix 派生（[4.3](#43-torch_mlirfxexport_and_import-转-mlir)）。
- 契约的**强制执行方**是 `compile-pytorch.py`（[第 41-46 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L41-L46)），而不是适配器——理解契约要先看验收人。
- 产物 `${name}-torch.mlir` 是一条纯文本 MLIR，含 `module` / `func.func` / `torch.aten.*` 算子，是后续整条降级链的起点。

---

## 7. 下一步学习建议

本讲产出的是 **torch 方言 MLIR**，下一讲就接手把它往下降级：

- **[u2-l3 torch-MLIR 前端降级：torch 方言到 Linalg](u2-l3-torch-mlir-frontend-lowering.md)**：看 `scripts/pipeline/torch_to_linalg.sh` 如何用 `torch-mlir-opt` 跑两条 backend pipeline，把本讲的 `torch.aten.matmul` 降成 Linalg-on-Tensors 上的 `linalg.matmul`。建议带着本讲产出的 `*-torch.mlir` 文本去对照阅读，会非常直观。
- 之后进入 [u3 单元](../)，看 CIRCT 把 Linalg 一路降到 SystemVerilog。

延伸阅读（源码）：
- 想看真实大模型适配器：再读一遍 [TinyStories/model_adapter.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/TinyStories/model_adapter.py)，注意 `use_cache=False`、`attn_implementation="eager"`、`local_files_only=True` 三个让模型「可被干净导出」的关键参数。
- 想看脚本如何被包成派生：[nix/pipeline.nix:15-34](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L15-L34) 的 `mkTorchInput` 与 `mkLinalgDerivation`。
