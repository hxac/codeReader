# compile-pytorch.py 与 torch.export

## 1. 本讲目标

本讲是 PyTorch 前端的「发动机」：逐行拆解整个流水线最上游的入口脚本 [scripts/compile-pytorch.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py)。它只有约 60 行，却完成了三件事——**把任意一个 PyTorch 模型，冻结成一张静态计算图，再翻译成 torch 方言的 MLIR 文本**。

学完本讲你应当能够：

1. 说清 `compile-pytorch.py` 为什么用 `importlib`「按文件路径动态加载适配器」，以及 `sys.path` 在这里的微妙作用（临时插入 + `finally` 回滚）。
2. 解释 `torch.export.export(..., strict=...)` 返回的 `ExportedProgram` 是什么，`example_inputs()` 为什么是「模具」，以及 `EXPORT_STRICT` 为什么 TinyStories 要设成 `False` 而 matmul 用默认 `True`。
3. 看懂 `torch_mlir.fx.export_and_import(exported, output_type="torch")` 产出的 torch 方言 MLIR 长什么样，以及为什么本脚本**故意停在 torch 方言、不继续往下降级**。
4. 把这三个步骤串成一条「一次 matmul 调用」的完整数据流，并实跑脚本、截取前 40 行输出。

本讲**承接** [u2-l1 模型与适配器契约](u2-l1-models-and-adapters.md)：上一讲确立了「适配器必须实现 `build_model` 与 `example_inputs`」的契约，本讲就去看**契约的验收方** `compile-pytorch.py` 是怎么消费这两个函数的——不重复适配器怎么写，只深挖消费方的三步机制。

---

## 2. 前置知识

### 2.1 从「带权重的 Python 对象」到「静态计算图」

一个 `torch.nn.Module`（比如 [src/matmul.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul.py) 里的 `MatmulModule`）本质是「一段会真正执行计算的 Python 代码」。它每次 `forward` 都在**解释执行**（eager 模式）：你能写 `if`、`for`、调任意库函数。

但硬件不能解释执行 Python。要变成 FPGA，必须先把模型脱成一张**静态计算图**——节点是算子（如 matmul、add），边是张量，所有动态控制流都被展开或固化。`torch.export` 就是 PyTorch 2.x 用来做这件事的官方 API，它返回的不是文件，而是一个内存对象 `ExportedProgram`。

> 关键认知：`torch.export.export` 产出的是一个**对象**（`ExportedProgram`），不是字符串、也不是文件。把图变成文本是后面 `torch_mlir` + `str(module)` 的事。

### 2.2 什么叫「用示例输入当模具」

`torch.export` 不是凭空知道输入张量形状的。你必须喂它一组**示例输入**（example inputs）。它拿着这组输入去跑一遍 `forward`，但**只记录算子序列**，并把示例输入的**形状和 dtype 烧进图里**当成常量。所以示例输入就是「模具」：它的形状决定输出图的形状。这也解释了 u2-l1 反复强调的铁律——`example_inputs()` 的形状/dtype/数量必须和 `forward` 形参一一对应。

### 2.3 MLIR、dialect、lowering 一句话回顾

承接 u2-l1：**lowering（降级）** 是把高层表示一步步翻译成更低层、更接近硬件的表示；每一步的「表示」叫一个 **dialect（方言）**。本讲产出的就是**最顶层的 torch 方言**（算子写成 `torch.aten.matmul` 这种），后续 [u2-l3](u2-l3-torch-mlir-frontend-lowering.md) 会把它降到 Linalg。

MLIR 在内存里是「操作（Operation）」组成的树；它的 Python API 约定：任何 IR 对象 `module`，`str(module)` 就能得到它的**可打印文本形式**。本讲会用到这一行：`mlir_text = str(module)`。

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
| [nix/models.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix) | 把脚本包成 Nix 派生、拼出真实命令行 | matmul 与 TinyStories 的 `torchInputCommand` |

一句话定位：`compile-pytorch.py` 是「适配器 → torch 方言 MLIR」的单站式转换器；它本身**不做任何降级**，降级是下一站 [u2-l3](u2-l3-torch-mlir-frontend-lowering.md) 的事。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应 `main()` 的三个关键步骤：

- **4.1 动态加载适配器模块**——`load_adapter` 如何按文件路径把任意 `.py` 当模块加载。
- **4.2 torch.export.export 导出图**——把模型冻结成 `ExportedProgram`，以及 `strict` 的作用。
- **4.3 torch_mlir.fx.export_and_import 转 MLIR**——把图翻译成 torch 方言文本并落盘。

### 4.1 动态加载适配器模块

#### 4.1.1 概念说明

`compile-pytorch.py` 通过命令行参数 `--adapter` 接收一个 `.py` 文件路径。它不预先知道是 matmul 还是 TinyStories——任何实现了契约（`build_model` + `example_inputs`）的文件都能被它消费。这种「运行时才知道加载谁」的需求，必须用**按路径动态加载**。

这里有个容易踩的坑：`importlib` 按路径加载某个文件时，只加载那一个文件；但该文件内部如果又 `import` 了**兄弟模块**（例如 [src/matmul_adapter.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py) 里 `from sim_utils import load_matmul_module`），Python 的 import 机制只会去 `sys.path` 里找——而适配器所在的目录默认并不在 `sys.path` 上，于是兄弟 import 会失败。脚本的 `sys.path.insert` 就是为解决这一点。

> 上一讲（u2-l1）已经讲过契约本身；这里只看「怎么把这个实现了契约的文件加载进来」。

#### 4.1.2 核心流程

加载一个适配器的步骤如下（伪代码）：

```text
输入: path = 用户传入的适配器文件路径（如 src/matmul_adapter.py）
1. 把 path 的父目录临时插到 sys.path 最前面  # 让兄弟 import 能解析
2. 用 importlib 按 path 造一个模块规格 spec（模块名取文件名 stem）
3. 由 spec 创建空模块对象
4. 执行 spec.loader.exec_module(module)         # 真正跑一遍这个 .py，填充 module
5. finally: 把刚才插入的父目录从 sys.path 移除   # 保持 sys.path 干净
6. 返回 module（此时它已带有 build_model / example_inputs 属性）
```

注意第 5 步的 `finally`：无论加载成功还是抛异常，都要把 `sys.path` 改动回滚，避免污染后续其他模型的加载——这是脚本级的「作用域卫生（hygiene）」。

#### 4.1.3 源码精读

加载逻辑全部在 `load_adapter` 函数里：

[scripts/compile-pytorch.py:14-27](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L14-L27) —— 把适配器文件按路径动态导入，并临时维护 `sys.path`：

```python
def load_adapter(path: Path) -> ModuleType:
    sys.path.insert(0, str(path.parent))                            # ① 临时让兄弟 import 可见
    spec = importlib.util.spec_from_file_location(path.stem, path)  # ② 模块名=文件名，位置=path
    if spec is None or spec.loader is None:
        raise SystemExit(f"unable to load adapter module from {path}")
    module = importlib.util.module_from_spec(spec)                  # ③ 创建空模块对象
    try:
        spec.loader.exec_module(module)                             # ④ 真正执行该 .py，填充属性
    finally:
        try:
            sys.path.remove(str(path.parent))                       # ⑤ 回滚 sys.path
        except ValueError:
            pass
    return module
```

几个要点：

- **[第 15 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L15) `sys.path.insert(0, ...)`**：`0` 表示插到最前，保证适配器自带的同名模块优先于全局环境里的同名包被选中。
- **[第 16 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L16) `path.stem`**：对 `matmul_adapter.py` 就是 `matmul_adapter`，它成为这个被加载模块的 `__name__`。两个参数分别是「模块叫什么名字」和「文件在哪」。
- **[第 19-21 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L19-L21) 真正执行**：`module_from_spec` 先造一个空模块对象，`spec.loader.exec_module` 才是「把这个 `.py` 文件从头到尾跑一遍」——适配器里的 `def build_model`、`EXPORT_STRICT = False` 等定义就是在这一刻生效，成为返回模块的属性。
- **[第 22-26 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L22-L26) 收尾**：`finally` 块**无论成功失败**都把第 1 步插入的目录移除；`except ValueError` 兜住「该目录不在列表里」的极端情况。

> **两个回滚细节**：(1) 必须用 `try/finally`，否则 `exec_module` 抛异常时就漏掉了回滚；(2) 用 `sys.path.remove(具体值)` 而不是 `sys.path.pop(0)`——因为 `exec_module` 期间适配器自己也可能改 `sys.path`，「栈顶」未必还是我们插入的那一项，按值删除才精准。

> 注意：matmul 适配器实际运行时能找到 `sim_utils`（它在 `sim/` 而不在 `src/`），靠的是 [nix/models.nix:13](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L13) 把 `simDir` 加进了 `PYTHONPATH`。脚本内的 `sys.path.insert` 只把 `src/`（适配器所在目录）临时加进去，管的是「同目录兄弟 import」；跨目录的 `sim_utils` 由 `PYTHONPATH` 负责。两条机制分工。

#### 4.1.4 代码实践

**实践目标**：验证 `sys.path.insert` 与 `finally` 回滚的必要性，理解「按值 remove vs 按位置 pop」。

**操作步骤**（源码阅读 + 推理型实践）：

1. 读 [scripts/compile-pytorch.py:14-27](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L14-L27)。
2. 打开 [nix/models.nix:11-17](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L11-L17)，看清 Nix 在调用前设了哪些环境变量。
3. 推理：假设把 [第 15 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L15) 的 `sys.path.insert` 注释掉，在「`PYTHONPATH` 没有预先配好」的情况下，加载一个含 `from sibling import x` 的适配器会发生什么？
4. 思考：为什么回滚用 `sys.path.remove(...)` 包在 `try/except ValueError` 里，而不是 `sys.path.pop(0)`？

**需要观察的现象 / 预期结论**：

- 删掉 insert 后，兄弟 import 会因 `ModuleNotFoundError` 失败（找不到 `sibling`）。
- 用 `pop(0)` 不安全：`exec_module` 期间如果适配器自己也改了 `sys.path`，「栈顶」可能已经不是我们插入的那一项，而 `remove(具体值)` 按值删除才精准。

> 待本地验证：在 `nix develop` 里手工触发一次 `load_adapter`，前后 `print(sys.path)` 能直观看到临时插入又被移除的目录条目。

#### 4.1.5 小练习与答案

**练习 1**：`spec_from_file_location(path.stem, path)` 的第一个参数 `path.stem` 影响什么？

**答案**：它是被加载模块的名字（即 `module.__name__`）。对 `matmul_adapter.py`，stem 是 `matmul_adapter`。它不影响文件从哪里读（那由第二个参数 `path` 决定），只影响模块的「身份」——让被加载模块的 `__name__` 与文件名一致，行为更接近「正常 import 这个文件」。

**练习 2**：如果用户传的 `--adapter` 指向一个不存在的文件，`load_adapter` 会怎样？

**答案**：`spec_from_file_location` 对不存在的路径仍可能返回非 None 的 spec，但在 [第 21 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L21) 的 `exec_module` 时会抛 `FileNotFoundError`/`ImportError`；脚本没有捕获它，所以会带着原始回溯退出（具体异常类型「待本地验证」）。

**练习 3**：这里加载好的 `module` 会被塞进 `sys.modules` 吗？为什么要这样设计？

**答案**：不会。这里只创建并执行模块对象、直接返回，**不**插入 `sys.modules`。也就是说，加载好的适配器是一个「一次性」对象，不会被 Python 的模块缓存机制记住——每次编译都重新、干净地加载，避免上次加载的残留状态影响本次。

---

### 4.2 torch.export.export 导出图

#### 4.2.1 概念说明

加载完适配器，下一步是把模型变成静态图。核心是 `torch.export.export`：它接收「模型 + 一组示例输入」，**用示例输入驱动一次符号化追踪**，产出一个 `ExportedProgram`——这是一个冻结的、不再依赖原始 Python 代码的计算图对象。

这里有两个工程细节必须讲清：

1. **`.eval()` 的意义**：`build_model(...)` 返回的模型在导出前被切到 eval 模式（见 [compile-pytorch.py:48](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L48) 的 `.eval()`）。这会关闭 dropout、把 BatchNorm 等切到推理行为，保证导出的图就是「推理时」的行为。matmul 适配器内部其实已经 `.eval()` 过一次，消费方再调一次是**幂等的防御式编程**（u2-l1 练习 3 讨论过）。

2. **`strict` 参数与 `EXPORT_STRICT`**：`torch.export.export` 有个 `strict` 开关。
   - `strict=True`（默认）：用 TorchDynamo 做符号追踪，把高层算子分解成更底层的 ATen 子集，并尝试把 Python 控制流固化或加守卫；它更精确，但对 Dynamo 不认识的 Python 写法会报错。
   - `strict=False`：走更宽容的记录路径，实际跑一遍 `forward`、把遇到的算子按原样记下来，适合那些 Dynamo 追不动的复杂模型。

脚本的精巧之处在于：`strict` 的值**不是写死的**，而是问适配器借的——`getattr(adapter, "EXPORT_STRICT", True)`，默认 `True`，但适配器可以设 `EXPORT_STRICT = False` 来改用宽容路径。一句话总结这次「决策权下放」：

> 严格模式能不能过，**取决于模型有多复杂**；而这个判断只有写适配器的人知道，所以消费方让适配器按需放宽，默认仍是最严格的 `True`。

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

注意：到这一步**还没有任何 MLIR**，`ExportedProgram` 仍是 PyTorch 自己的图对象，且**没有产生任何文件**——变成文本并落盘是 4.3 的事。

#### 4.2.3 源码精读

先看脚本如何「问适配器要契约」并做硬校验（u2-l1 已讲过，这里只确认它就是导出的前置关卡）：

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

三个细节：

1. **[第 48 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L48) `.eval()`**：matmul 适配器内部已 `.eval()`，消费方再调一次是幂等的防御式编程；它的意义在 TinyStories 上更明显——消费方不依赖「适配器作者一定记得调」。
2. **[第 51 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L51) `tuple(example_inputs())`**：`example_inputs()` 已返回元组，外层再套 `tuple(...)` 确保它被固化成「传给 `forward` 的位置参数元组」，数量必须与 `forward` 形参对齐。
3. **[第 52 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L52) `strict=getattr(adapter, "EXPORT_STRICT", True)`**：4.2.1 节讲过的「决策权下放」。默认值 `True` 写在**消费方**，所以适配器不写 `EXPORT_STRICT` 就走最严格——matmul 正是靠这个默认值。

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

**实践目标**：直观感受「示例输入 = 模具」与 `strict` 的权衡。

**操作步骤**（源码阅读 + 推理型）：

1. 打开 [src/matmul_adapter.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py)，确认它**没有**写 `EXPORT_STRICT`；打开 [TinyStories/model_adapter.py:9](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/TinyStories/model_adapter.py#L9)，确认它写了 `EXPORT_STRICT = False`。
2. 假设把 matmul 的 `example_inputs` 改成只返回**一个**张量（删掉第二个），即与 `forward(self, a, b)` 的双入参不匹配。推理：`torch.export.export` 会发生什么？
3. 再假设把 matmul 的 `dtype=torch.int32` 改成 `dtype=torch.float32`：导出图里张量类型会变成什么？

**需要观察的现象 / 预期结论**：

| 改动 | 预期后果 |
|------|----------|
| matmul 加 `EXPORT_STRICT = False` | 仍应成功——「放宽严格度」不会让简单模型失败；算子仍是 `torch.aten.matmul` |
| TinyStories 去掉 `EXPORT_STRICT = False`（强制 `True`） | 很可能在 Dynamo 校验阶段报错——大模型通不过严格追踪 |
| `example_inputs` 只返回一个张量 | `torch.export` 因「位置实参数量与 `forward` 形参不一致」报错 |
| dtype 改成 `float32` | 导出图里张量类型变成 `!torch.tensor<[16]xf32>`，影响下游整条链的类型推导 |

> 待本地验证：上述「预期后果」是依据 PyTorch/torch-mlir 通用行为给出的推理，具体报错文本以本地 `compile-pytorch.py` 输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么导出前要 `.eval()`？能不能省略？

**答案**：`.eval()` 把模块切到推理模式，关闭 dropout 等训练期随机行为，并让 BatchNorm/LayerNorm 等用推理统计量。省略会导致导出的图可能含训练期算子（如 dropout），这与「部署推理」语义不符，也无法稳定复现。不过 `.eval()` 是幂等的，消费方多调一次只是防御。

**练习 2**：`strict=True` 和 `strict=False` 各自适合什么场景？为什么消费方不写死？

**答案**：`strict=True`（Dynamo 追踪）适合结构简单、Dynamo 能完整分析的模型（matmul），产出更规整；`strict=False`（宽容记录）适合 Dynamo 追不动的复杂模型（TinyStories-1M）。不写死是因为「该不该严格」是**模型相关**的判断，只有写适配器的人清楚。`getattr(adapter, "EXPORT_STRICT", True)` 是「默认最严、按需放宽」的折中。

**练习 3**：`torch.export.export` 返回的 `exported` 是字符串吗？

**答案**：不是。它是一个 `torch.export.ExportedProgram` 对象（内存中的计算图）。把图变成字符串是后面 `torch_mlir` 翻译 + `str(module)` 的事（见 4.3）。理解这一点能避免「为什么这里没看到 MLIR 文本」的困惑。

---

### 4.3 torch_mlir.fx.export_and_import 转 MLIR

#### 4.3.1 概念说明

这是本讲**全新的**内容（u2-l1 只到「门口」）。`ExportedProgram` 还是 PyTorch 的私人物品，硬件工具链看不懂。最后一步用 **torch-MLIR** 把它翻译成 **MLIR 文本**，写进文件。它做的事情可以一句话概括：**遍历 `ExportedProgram` 里的每个 ATen 算子，逐个翻译成对应的 `torch.aten.<op>` MLIR 操作，组装成一个 MLIR module。**

注意用的是 `torch_mlir.**fx**` 子模块——`fx` 表示这是**基于 `torch.export`（FX 图 / `ExportedProgram`）的新一代导入器**，区别于 torch-MLIR 早期基于 TorchScript 的 `torch_mlir.compile` 路径。本仓库既然已经用 `torch.export` 产出 `ExportedProgram`，自然要用 fx 导入器来接。

关键在 `output_type="torch"` 这个选择。torch-MLIR 支持几种「降到哪个方言为止」的输出类型，自上而下越来越底层：

| `output_type` | 停在哪个方言 | 算子长这样 | 降得多少 |
|---------------|--------------|-----------|----------|
| `"torch"` | torch 方言（最高层） | `torch.aten.matmul` | **最少**（只翻译，不降级） |
| `"tosa"` | TOSA 方言 | `tosa.matmul` | 中等 |
| `"linalg-on-tensors"` | Linalg 方言 | `linalg.matmul` | 较多 |

本脚本**故意只选 `"torch"`**——即只翻译、不降级。为什么不下沉到 Linalg？因为降级是下一站 [scripts/pipeline/torch_to_linalg.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/torch_to_linalg.sh)（详见 [u2-l3](u2-l3-torch-mlir-frontend-lowering.md)）用 `torch-mlir-opt` 跑 backend pipeline 干的活。把「捕获成 torch 方言」和「降级到 Linalg」拆成两个独立的、可被 Nix 缓存的派生（见 [nix/pipeline.nix:15-34](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L15-L34) 的 `mkTorchInput` 与 `mkLinalgDerivation`），改其中一步不必重跑另一步——这正是整个仓库「各站只降一段」的设计哲学。

#### 4.3.2 核心流程

```python
module     = export_and_import(exported, output_type="torch")  # ① ExportedProgram → torch 方言 MLIR 模块
mlir_text  = str(module)                                        # ② ModuleOp 序列化成文本 IR
args.out.write_text(mlir_text, encoding="utf-8")                # ③ 写到 --out（派生产物 *-torch.mlir）
print(mlir_text)                                                # ④ 同时打印到 stdout
```

数据形态变化：

\[
\text{ExportedProgram} \;\xrightarrow{\text{export\_and\_import}(\text{output\_type}=\text{"torch"})}\; \text{MLIR ModuleOp} \;\xrightarrow{\text{str()}}\; \text{torch 方言文本}
\]

两个落盘动作（写文件 + 打印到 stdout）值得留意——它们解释了 Nix 为什么要 `>/dev/null`。

#### 4.3.3 源码精读

先看 import，它决定用的是哪一代导入器：

[scripts/compile-pytorch.py:10](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L10) —— 引入 fx 导入器：

```python
from torch_mlir.fx import export_and_import
```

从 `torch_mlir.fx`（而不是顶层 `torch_mlir`）import，明确走的是「基于 `torch.export` 的导入器」。这是整条降级链的「PyTorch → MLIR」转换点。

再看翻译与落盘这四行：

[scripts/compile-pytorch.py:54-57](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L54-L57) —— 翻译成 torch 方言 MLIR 并落盘：

```python
module = export_and_import(exported, output_type="torch")
mlir_text = str(module)
args.out.write_text(mlir_text, encoding="utf-8")
print(mlir_text)
```

四个细节：

1. **[第 54 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L54) `export_and_import(exported, output_type="torch")`**：第一个参数是 4.2 节得到的 `ExportedProgram`；`output_type="torch"` 让它停在 torch 方言。返回值 `module` 是一个 MLIR module 对象（不是字符串）。
2. **[第 55 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L55) `str(module)`**：2.3 节讲过的 MLIR Python API 约定——`str(module)` 把内存里的 module 对象序列化成可读文本。这是 IR 对象变成字符串的唯一手段。
3. **[第 56 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L56) `args.out.write_text(...)`**：把文本写进 `--out` 指定的文件。这个文件就是降级链下一站 `torch_to_linalg.sh` 的输入。
4. **[第 57 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L57) `print(mlir_text)`**：把**同一份**文本也打印到标准输出。这正是 [nix/models.nix:16](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L16) 和 [nix/models.nix:31](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L31) 都用 `>/dev/null` 的原因——构建时只要 `--out` 文件，刷屏的 stdout 是噪声；而本地调试时**保留 stdout** 就能直接看到 MLIR。

产物的去向：`args.out` 在 Nix 里被绑成派生输出 `${name}-torch.mlir`（见 [nix/pipeline.nix:20](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L20) 的 `runCommand "${name}-torch.mlir"`），也就是 [nix/models.nix:14-16](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L14-L16) 里 `python ${compilePyTorch} --adapter ... --out "$out"` 的 `$out`。

产出的 MLIR 文本大致结构如下（以 matmul 为例的**示意**，精确文本「待本地验证」）：

```mlir
module {                                  # 顶层模块（torch-mlir 命名随版本：@main 或 @__torch__.<类名>）
  func.func @main(%arg0 : !torch.tensor<[16]xi32>, %arg1 : !torch.tensor<[16]xi32>)
      -> !torch.tensor<...> {
    %0 = torch.aten.matmul %arg0, %arg1 : !torch.tensor<[16]xi32>, !torch.tensor<[16]xi32> -> !torch.tensor<...>
    return %0 : !torch.tensor<...>
  }
}
```

几个特征要记住，下一讲会反复用到：

- 顶层是一个 `module`，里面是一个入口 `func.func`；具体符号名（`@main`、`@__torch__.MatmulModule` 等）**由 torch-mlir 版本决定**，以本地输出为准。
- 张量类型写成 `!torch.tensor<[16]xi32>`——这个 `[16]` 和 `i32` 正是从 4.2 的「模具」烧进来的。
- 算子写成 `torch.aten.matmul` 这种 **torch 方言** 形式，这正是 `output_type="torch"` 的产物，也是 [u2-l3](u2-l3-torch-mlir-frontend-lowering.md) 要继续往下降级的起点。

#### 4.3.4 代码实践

**实践目标**：把 `compile-pytorch.py` 跑在 matmul 适配器上，截取 torch 方言 MLIR 前 40 行，确认它停在 torch 方言、且形状来自模具。

**操作步骤**（在 `nix develop` 环境里）：

1. 读 [nix/models.nix:11-17](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L11-L17)，看清 Nix 里 matmul 的真实命令：设 `MATMUL_PY`、设 `PYTHONPATH`、再调用脚本（`>/dev/null`）。
2. 进入开发 shell：`nix develop`（u1-l3；devShell 含 `torchMlir`、`pythonWithTorch`，见 [flake.nix:761-769](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L761-L769)）。
3. 在仓库根目录，**复现上面的命令但去掉 `>/dev/null`** 以便看到 MLIR，并截前 40 行：

   ```bash
   # 1) 让 matmul 适配器知道从哪加载模型（u2-l1 的 MATMUL_PY 机制）
   export MATMUL_PY="$PWD/src/matmul.py"
   # 2) 拼好 PYTHONPATH：src、sim、以及 torch-mlir 的 site-packages
   #    （用 import 定位 torch_mlir 所在，避免记死 python 版本号）
   export PYTHONPATH="$PWD/src:$PWD/sim:$(python -c 'import torch_mlir,os;print(os.path.dirname(os.path.dirname(torch_mlir.__file__)))'):${PYTHONPATH:-}"
   # 3) 跑消费方，stdout 直接喂给 head -40
   python scripts/compile-pytorch.py \
     --adapter src/matmul_adapter.py \
     --out /tmp/matmul-torch.mlir | head -n 40
   ```

4. 在前 40 行里圈出并记录：
   - 顶层 `module` 声明（记下实际符号名，是 `@main` 还是 `@__torch__.MatmulModule`，以本地为准）；
   - 入口 `func.func` 的入参类型（应为两个 `!torch.tensor<[16]xi32>`，对应 [src/matmul_adapter.py:15-19](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul_adapter.py#L15-L19) 喂的模具）；
   - `torch.aten.matmul` 算子（[src/matmul.py:5](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/src/matmul.py#L5) 的 `torch.matmul` 翻译后的样子）；
   - **确认全文不含** `linalg.`、`scf.`、`cf.` 等更低层方言——`output_type="torch"` 确实停在了 torch 方言。

**需要观察的现象**：

- stdout 与 `/tmp/matmul-torch.mlir` 内容一致（脚本同时 `print` 和 `write_text`，[第 56-57 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L56-L57)）。
- 输出含 `torch.aten.matmul` 与 `!torch.tensor<[16]xi32>` 类型，且无更低层结构。

**预期结果**：你拿到一份只在 torch 方言里的 matmul MLIR，能指出顶层 module 与 `torch.aten.matmul` 算子；并理解「这一站只翻译、不降级」，真正的降级从 [u2-l3](u2-l3-torch-mlir-frontend-lowering.md) 开始。

> 待本地验证：本实践未在当前环境实际运行；确切命令、PYTHONPATH 中 torch-mlir 的真实 site-packages 路径、以及 MLIR 里的符号名/属性写法，请以本地 `nix develop` 输出为准。若 `import torch_mlir` 报错，说明 torch-mlir site-packages 未在路径上——这正是 Nix 在 `torchMlirPythonPath`（[nix/models.nix:5-6](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L5-L6)）替你拼好的那一项。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `output_type="torch"` 改成 `"linalg-on-tensors"`，本脚本的产物会发生什么变化？对后续脚本意味着什么？

**答案**：翻译器会一口气把 torch 方言**继续往下降**到 Linalg-on-Tensors，产物里不再有 `torch.aten.*`，取而代之的是 `linalg.*`。这意味着 `compile-pytorch.py` 越权做了「下一站的工作」，而后续的 `torch_to_linalg.sh`（u2-l3）将无活可干、甚至因输入方言不对而报错。这正是为什么本讲坚持 `output_type="torch"`：**每个脚本只降一段，职责单一，便于定位和缓存**。

**练习 2**：`export_and_import` 返回的 `module` 与 `str(module)` 的关系是什么？`module` 是字符串吗？

**答案**：`module` 是 MLIR 的内存 IR 对象（`ModuleOp`，树形 Operation），**不是**字符串。`str(module)` 是它的可打印文本形式。MLIR Python API 不允许直接把 IR 对象「当字符串用」，必须显式 `str()` 才能得到文本——然后才能 `write_text` 落盘或 `print` 显示。

**练习 3**：为什么 Nix 在调用时加 `>/dev/null`，而你手动跑时反而不加？

**答案**：因为 [第 57 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L57) 会把整份 MLIR 打印到 stdout。Nix 构建只要 `--out` 文件（[第 56 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L56)），刷屏的 stdout 是噪声，所以重定向掉；而你手动调试时，正是想看这份 stdout，所以保留它、甚至 `| head -40` 截取。

---

## 5. 综合实践

**贯穿任务：把 `compile-pytorch.py` 真正跑在 matmul 适配器上，画出数据流图，并截取 torch 方言 MLIR 的前 40 行。**

这个任务同时调动本讲三个模块：动态加载 matmul 适配器（4.1）、用 `torch.export` 导出 `MatmulModule`（4.2）、用 `export_and_import` 翻译成 torch 方言（4.3）。

**第 1 步：画数据流图。** 以 [compile-pytorch.py 的 `main()`](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L30-L57) 为线索，画出「从命令行参数到 MLIR 文件」的流程，特别标清三种中间物：

```text
--adapter (命令行)  ──load_adapter──►  adapter 模块对象（含 build_model / example_inputs）
--model-path (命令行) + adapter.build_model  ──►  model (nn.Module)
adapter.example_inputs  ──torch.export.export──►  ExportedProgram（内存对象，不落盘）
ExportedProgram  ──export_and_import(output_type="torch")──►  MLIR module  ──str()──►  文本
文本  ──write_text──►  --out 文件；同时 ──print──►  stdout
```

**第 2 步：跑通 matmul。** 按 4.3.4 的步骤把 matmul 适配器跑起来，截前 40 行 MLIR，标出顶层 `module` 与 `torch.aten.matmul`。

**第 3 步：改一个参数观察行为。** 把 matmul 适配器临时加上一行 `EXPORT_STRICT = False`，重跑一次，对比输出是否仍然成功、算子是否仍是 `torch.aten.matmul`；再用 `getattr` 求值规则解释现象。

**参考答案要点**：

1. 数据流图的关键是「**每个阶段只产出一种中间物，且都由消费方主导**」：`load_adapter` 产「模块对象」；契约检查（[41-46 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L41-L46)）确保它有 `build_model` / `example_inputs`；`torch.export.export` 产 `ExportedProgram`（内存对象，不落盘）；`export_and_import` 产 MLIR module，`str` + `write_text` 才落盘到 `--out`。命令行三参 `--adapter` / `--model-path` / `--out` 分别对应「输入模型来源 / 模型路径 / 输出文件」。
2. 跑通后应看到 torch 方言 MLIR，含 `torch.aten.matmul` 与 `!torch.tensor<[16]xi32>` 类型，无更低层方言。
3. matmul 加 `EXPORT_STRICT = False` 后**仍应成功**——「放宽严格度」不会让简单模型失败，算子仍是 `torch.aten.matmul`。这印证 `strict` 只影响**导出校验**、不影响翻译出的算子种类；它和 TinyStories 必须 `False` 形成对照——**复杂模型才需要放宽**。

> 反思题：把输出与 [TinyStories/model_adapter.py:24](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/TinyStories/model_adapter.py#L24) 的模具 `(1,1) long` 对照——若改跑 TinyStories，入参类型应变成 `!torch.tensor<[1,1]xi64>`，算子也会多得多（整个 transformer）。这说明同一个脚本、同一个流程，**换适配器就能换模型**，这正是「模型/适配器分离」的价值。

> 待本地验证：综合实践含实际运行步骤，未在当前环境执行；MLIR 符号名、是否需补充环境变量等请以本地 `nix develop` 输出为准。若想走更慢但完全可复现的路线：构建一条下游已暴露的派生（如 `nix build .#matmul-selftest-bitstream`，见 [flake.nix:792-798](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L792-L798)），再 `find /nix/store -name '*-torch.mlir'` 定位 torch 阶段产物用 `head -n 40` 查看。注意：`.#matmul.pipeline.torch` 这类中间阶段**并未**作为独立 `packages` 暴露，不能直接 `nix build` 它。

---

## 6. 本讲小结

- [scripts/compile-pytorch.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py) 是流水线最上游入口：**适配器 → torch 方言 MLIR**，它本身**不做任何降级**。
- 它用 `importlib` 按文件路径**动态加载适配器**，并用 `sys.path.insert` + `try/finally` 回滚来保证兄弟 import 可解析且不污染全局路径（[4.1](#41-动态加载适配器模块)、[第 14-27 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L14-L27)）。
- 它用 `torch.export.export(model, tuple(example_inputs()), strict=...)` 把模型冻结成静态图 `ExportedProgram`；`example_inputs()` 是「模具」，形状/dtype 被烧进图里；`strict` 由适配器的 `EXPORT_STRICT` 决定，matmul 用默认 `True`、TinyStories 用 `False`（[4.2](#42-torchexportexport-导出图)、[第 48-53 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L48-L53)）。
- 它用 `export_and_import(exported, output_type="torch")` 翻译成 **torch 方言** MLIR，故意停在最高层，把降级留给下一站，让两步各自成为可缓存的 Nix 派生（[4.3](#43-torch_mlirfxexport_and_import-转-mlir)、[第 54-57 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L54-L57)）。
- 契约的**强制执行方**是 `compile-pytorch.py`（[第 41-46 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L41-L46)），而不是适配器——理解契约要先看验收人。
- 脚本同时把 MLIR `write_text` 到 `--out` 文件**和** `print` 到 stdout；Nix 构建用 `>/dev/null` 屏蔽 stdout，本地调试则保留它直接看 MLIR（[第 56-57 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py#L56-L57) vs [nix/models.nix:16](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L16)）。

---

## 7. 下一步学习建议

本讲产出的是 **torch 方言 MLIR**，下一讲就接手把它往下降级：

- **[u2-l3 torch-MLIR 前端降级：torch 方言到 Linalg](u2-l3-torch-mlir-frontend-lowering.md)**：看 [scripts/pipeline/torch_to_linalg.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/torch_to_linalg.sh) 如何用 `torch-mlir-opt` 跑两条 backend pipeline，把本讲的 `torch.aten.matmul` 降成 Linalg-on-Tensors 上的 `linalg.matmul`。建议带着本讲产出的 `*-torch.mlir` 文本去对照阅读，会非常直观。
- 之后进入第三单元，看 CIRCT 把 Linalg 一路降到 SystemVerilog。
- **u7-l1（注册新模型）**：当你想把自己写的适配器接进流水线，回头看 [nix/models.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix)，会发现本讲讲的 `MATMUL_PY` / `PYTHONPATH` / `>/dev/null` 都是注册新模型时要照搬的环境编排。

**建议的源码阅读顺序**：先重读 [scripts/compile-pytorch.py](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/compile-pytorch.py) 全文（只有约 60 行），再对照 [nix/models.nix:8-33](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix#L8-L33) 看脚本被「真实」调用时的环境装配，形成「代码逻辑 → 构建编排」的完整闭环。
