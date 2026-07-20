# TorchGen 代码生成机制

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚为什么 PyTorch 要用「代码生成」来管理几千个算子，而不是手写绑定。
- 跟踪一条「`native_functions.yaml` 中的一行 schema → 一组 `.h/.cpp` 文件」的完整路径。
- 读懂 `torchgen/gen.py` 的顶层入口 `main()` 是如何分阶段生成 headers、sources、yaml 的。
- 理解 `torchgen/model.py` 中 `NativeFunction` 与 `FunctionSchema` 这两个数据类的关键字段与「无损往返（lossless roundtrip）」设计。
- 解释 `torchgen/native_function_generation.py` 为何能根据一个算子自动「长出」functional / out 变体，并据此把相似算子分组。

本讲承接 [u3-l1 native_functions.yaml 算子模式定义](u3-l1-native-functions-yaml-schema.md)：上一讲讲了「单一事实来源 YAML 长什么样」，本讲讲「这张 YAML 是如何被吃进去、又被吐出成千上万行 C++ 的」。

## 2. 前置知识

在进入源码前，先用三个直觉建立心智模型。

### 2.1 为什么需要代码生成

PyTorch 有 3000 多个公开算子，每个算子都要同时拥有：

- 一份 C++ 公开 API 声明（`at::add`）。
- 一份 Tensor 方法（`Tensor::add`）。
- 一份注册到 Dispatcher 的 schema 与 kernel。
- 可能的 out 变体、inplace 变体、functional 变体、反向（autograd）公式、vmap plumbing、functionalization 包装……

如果全部手写，光是「加一个算子」就要改十几个文件、几百行样板代码，而且极易写错。`torchgen` 的作用就是：**让人类只维护一份 YAML，由机器保证所有派生产物彼此一致**。这正是上一讲强调的「单一事实来源」原则在工程上的落地。

### 2.2 代码生成 ≠ 运行时

`torchgen/` 是一个**构建期**工具，它本身不参与 `import torch` 时的运行时。它的输入是 `native_functions.yaml` + `tags.yaml`，输出是落到 `build/aten/src/ATen/` 下的一堆 `.h`/`.cpp`/`.cu`/`.yaml` 文件，这些文件再被 CMake 编译进 `libtorch` / `libtorch_python`。换句话说，`torchgen` 是「编译器的编译器」。

### 2.3 柯里化（curried）生成器风格

`gen.py` 里有大量形如这样的函数/类：

```python
@dataclass(frozen=True)
class ComputeFunction:
    @method_with_native_function
    def __call__(self, f: NativeFunction) -> str | None:
        ...
```

它接受「要生成什么」作为配置（这里是 `self`），返回一个「吃 `NativeFunction`、吐字符串」的 callable。这种柯里化风格让你可以用 `mapMaybe(ComputeFunction(), native_functions)` 把它套到整个算子列表上。`mapMaybe` 会自动过滤掉返回 `None` 的项（表示这个算子不需要这种产物）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [torchgen/gen.py](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/gen.py) | 代码生成主入口：解析 YAML、调度所有「生成器」、把产物写到磁盘。 |
| [torchgen/model.py](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/model.py) | 数据模型：`NativeFunction` / `FunctionSchema` / `Arguments` / `DispatchKey` 等，把 YAML 文本解析成强类型 dataclass。 |
| [torchgen/native_function_generation.py](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/native_function_generation.py) | 「自动补全」规则：根据一个算子的 inplace/out 变体，推导生成缺失的 functional/out 变体，并把相似算子分组成 `NativeFunctionsGroup`。 |
| [torchgen/utils.py](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/utils.py) | `FileManager`：模板替换、按 shard 写文件、只在内容变化时落盘。 |
| [torchgen/context.py](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/context.py) | `with_native_function` 等装饰器：为每个算子建立「出错时能定位到 YAML 行号」的上下文。 |
| [aten/src/ATen/templates/](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/templates) | 一堆 `${占位符}` 模板文件（`Operators.h`、`Function.h`、`TensorBody.h` 等），生成器把字符串填进去。 |
| [cmake/Codegen.cmake](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/cmake/Codegen.cmake) | 构建侧：用 `python -m torchgen.gen` 调起生成，并把产物登记为 CMake 依赖。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块拆分：

- **4.1 数据模型（model.py）**：YAML 如何变成 `NativeFunction` / `FunctionSchema`。
- **4.2 顶层生成入口（gen.py）**：`main()` 如何分阶段把数据模型渲染成文件。
- **4.3 自动补全规则（native_function_generation.py）**：codegen 如何「无中生有」地补出缺失的算子变体。

### 4.1 数据模型：YAML 到 NativeFunction / FunctionSchema

#### 4.1.1 概念说明

`model.py` 是整个 codegen 的「语义层」。它遵循三条设计原则（见文件顶部注释）：

1. **不以 C++ 类型作为内部表示**：内部数据结构围绕 JIT schema 表达，避免「读进来立刻又翻译成 C++ 类型」的老问题。
2. **用 dataclass 而非 dict/string**：每个有意义的实体都有自己的类，带有强语义不变量。
3. **无损往返（lossless roundtrip）**：从字符串 parse 成对象后，`str(对象)` 必须能精确还原原字符串——parse 时会 assert 这一点。这迫使数据表示忠实记录所有语法细节。

最关键的两个类是 `NativeFunction`（一条 YAML 条目）与 `FunctionSchema`（一条算子的类型签名）。

#### 4.1.2 核心流程

把 `add.Tensor(Tensor self, Tensor other, *, Scalar alpha=1) -> Tensor` 这一行解析成对象，分三步：

1. `NativeFunction.from_yaml` 拿到整条 YAML dict，先从中弹出 `func` 字符串。
2. 用 `NamespaceHelper` 切出命名空间（`aten`）与 schema 字符串，交给 `FunctionSchema.parse`。
3. `FunctionSchema.parse` 用一个正则切成「名字 / 参数 / 返回」三段，分别交给 `OperatorName.parse` / `Arguments.parse` / `parse_returns`，最后 assert `str(r) == func` 保证无损。

`NativeFunction` 本身则把 schema 连同 `variants`、`dispatch`、`structured`、`tags` 等字段，以及一批「派生布尔量」（如 `has_composite_implicit_autograd_kernel`）一起装进一个 frozen dataclass。

#### 4.1.3 源码精读

`NativeFunction` 类定义与字段说明：

[torchgen/model.py:506-617](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/model.py#L506-L617) —— `NativeFunction` 是 frozen dataclass，关键字段包括 `namespace`、`func: FunctionSchema`、`variants`、`structured` / `structured_delegate`、`tags`、`loc: Location`（YAML 行号，用于报错），以及四个 `has_composite_*_kernel` 布尔标记。

注意它**不直接内嵌 dispatch 表**——`from_yaml` 会把 dispatch 信息单独整理成 `{DispatchKey: {OperatorName: BackendMetadata}}` 返回出去，交给 `BackendIndex` 按后端索引（见 [torchgen/model.py:983-987](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/model.py#L983-L987) 的注释「We aren't going to store dispatch metadata inline in NativeFunctions」）。

`from_yaml` 的解析入口：

[torchgen/model.py:624-635](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/model.py#L624-L635) —— 弹出 `func` 字符串，用 `NamespaceHelper.from_namespaced_entity`（只允许一级命名空间，如 `aten::add`）切出命名空间，再交给 `FunctionSchema.parse`。

`FunctionSchema` 与它的无损 parse：

[torchgen/model.py:1542-1557](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/model.py#L1542-L1557) —— `decl_re` 正则把 schema 切成 name/args/returns 三段；最后一行 `if str(r) != func: raise AssertionError` 正是「无损往返」断言，任何 parse 与 print 不一致的改动都会立即失败。

`Arguments.parse` 把参数列表切成更细的结构（self / positional / kwarg_only / out / tensor_options）：

[torchgen/model.py:2541-2622](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/model.py#L2541-L2622) —— `_preparse` 先按 `*` 与「是否 mutable 注解」把参数归入 positional / kwarg_only / out 三类；第二阶段再分离 `self` 参数和连续出现的 `dtype/layout/device/pin_memory`（合并为 `TensorOptionsArguments`）。

`SchemaKind` 把一个 schema 归类为五种之一，是后面分组与生成的基础：

[torchgen/model.py:1717-1759](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/model.py#L1717-L1759) —— `kind()` 的判定优先级：inplace > scratch > out > mutable > functional。注意同名算子的不同变体（如 `add.Tensor` functional、`add_.Tensor` inplace、`add.out` out）会被归为不同 SchemaKind。

#### 4.1.4 代码实践

**实践目标**：用 `torchgen` 自己的 API 解析一条真实 schema，亲眼看到「字符串 → 对象 → 还原字符串」的无损往返。

**操作步骤**（在仓库根目录执行）：

```bash
python -c "
from torchgen.model import FunctionSchema, NativeFunction
s = FunctionSchema.parse('add.Tensor(Tensor self, Tensor other, *, Scalar alpha=1) -> Tensor')
print('name       :', s.name)
print('overload   :', s.name.overload_name)
print('kind       :', s.kind())
print('positional :', [a.name for a in s.arguments.flat_positional])
print('kwarg_only :', [a.name for a in s.arguments.flat_kwarg_only])
print('roundtrip  :', str(s))
"
```

**需要观察的现象**：

1. `kind()` 应为 `SchemaKind.functional`。
2. `flat_positional` 应包含 `['self', 'other']`，`flat_kwarg_only` 应包含 `['alpha']`（因为 `*` 之后的参数是 kwarg-only）。
3. `str(s)` 输出的字符串应与输入**逐字符相同**——这就是无损往返断言在运行时的体现。

**预期结果**：脚本不抛异常，且最后一行打印的 schema 与输入完全一致。如果你把输入改成多一个空格（如 `Tensor  self`），`parse` 会直接抛 `AssertionError`，证明这个不变量是被强制维护的。

> 说明：此实践为「源码阅读 + 最小调用」型，不依赖 GPU 或完整编译，只需能 import `torchgen`（仓库根目录在 `PYTHONPATH` 即可）。

#### 4.1.5 小练习与答案

**练习 1**：`add.out(Tensor self, Tensor other, *, Scalar alpha=1, Tensor(a!) out) -> Tensor(a!)` 的 `kind()` 是什么？`arguments.out` 里包含哪些参数？

**参考答案**：`kind()` 是 `SchemaKind.out`（因为有 mutable 的 kwarg-only 参数 `Tensor(a!) out`）。`arguments.out` 包含一个参数 `out`，其 `annotation.is_write == True`。

**练习 2**：为什么 `NativeFunction` 要把 dispatch 信息单独放到 `BackendIndex`，而不是作为自己的字段？

**参考答案**：因为外部后端（如 XLA）可以在 `native_functions.yaml` 之外**追加**自己的 dispatch 条目，且可以让同一算子在不同后端上独立选择是否 structured。把 dispatch 按 `DispatchKey` 索引到 `BackendIndex`，才能支持「后端按需扩展」而不污染 `NativeFunction` 这一无后端语义的核心模型。

---

### 4.2 顶层生成入口：gen.py 的 main() 与分阶段渲染

#### 4.2.1 概念说明

`gen.py` 是整个 codegen 的总调度。它的职责可以概括为一句话：**「把 `NativeFunction` 列表，按若干个生成器（generator），渲染成若干份模板文件」**。

这里有两个关键抽象：

- **生成器（generator）**：一个吃 `NativeFunction`（或 group）、吐一段 C++ 字符串的 callable。典型如 `ComputeFunction`、`ComputeOperators`、`ComputeTensorMethod`、`RegisterSchema`。它们大多用 `@method_with_native_function` 或 `@with_native_function` 包装，自动建立报错上下文。
- **FileManager**：负责「模板 + 环境字典 → 落盘文件」，并且只在内容真的变化时才写盘（`_write_if_changed`），从而避免无谓的重新编译。

`main()` 把工作分成三个可独立选择的阶段：`headers`、`sources`、`declarations_yaml`，由 `--generate` 参数控制。

#### 4.2.2 核心流程

`main()` 的整体编排如下（伪代码）：

```
1. 解析命令行参数（--source-path / --install-dir / --per-operator-headers / --rocm / --mps ...）
2. parse_native_yaml(...) -> ParsedYaml(native_functions, backend_indices)
3. get_grouped_native_functions(...)        # 4.3 讲：把相似算子分组成 NativeFunctionsGroup
4. 按 dispatch key 的支持情况调整 functions_keys / ignore_keys
5. 创建若干 FileManager（core_fm / cpu_fm / cuda_fm / ops_fm / aoti_fm / headeronly_fm）
6. if "sources"     in generate: gen_source_files(...)      # 写 Register*.cpp / Operators.cpp / ...
7. if "headers"     in generate: gen_headers(...)            # 写 Operators.h / Functions.h / TensorBody.h / ...
8. if "declarations_yaml" in generate: gen_declarations_yaml(...)  # 写 Declarations.yaml
```

而构建侧（CMake）通过 `python -m torchgen.gen` 调起它，并把产物登记成 CMake 依赖。

#### 4.2.3 源码精读

文件顶部的「设计自述」值得先读，它点明了 model / api / gen 的三层分工：

[torchgen/gen.py:104-124](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/gen.py#L104-L124) —— 注释说明：`model` 是 YAML 的数据模型；`api` 负责把 schema 翻译成**三种不同的 C++ API**（公开 C++ API、dispatcher API、legacy dispatcher API）；本文件只做「调度 + 渲染」。

YAML 解析入口（带行号追踪与全局缓存）：

[torchgen/gen.py:135-145](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/gen.py#L135-L145) —— `LineLoader` 是自定义 YAML loader，它在每条 mapping 里塞一个 `__line__` 字段记录原始行号，这样后续报错能精确指向 YAML 的第几行。`ParsedYaml = namedtuple(...)` 定义了 parse 的返回结构：算子列表 + 按后端索引的 kernel 表。

[torchgen/gen.py:253-280](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/gen.py#L253-L280) —— `parse_native_yaml` 读文件、调 `parse_native_yaml_struct`、并用 `_GLOBAL_PARSE_NATIVE_YAML_CACHE` 做全局缓存（同一 path 只解析一次，被多个下游工具复用）。

`main()` 的三阶段调度：

[torchgen/gen.py:3027-3073](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/gen.py#L3027-L3073) —— 分别在 `options.generate` 包含 `sources` / `headers` / `declarations_yaml` 时调用 `gen_source_files` / `gen_headers` / `gen_declarations_yaml`。注意它们共用同一份已分组的 `grouped_native_functions`，保证三个阶段看到一致的算子集合。

一个具体生成器：`ComputeFunction`（生成公开 C++ 函数式 API `at::add`）：

[torchgen/gen.py:710-753](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/gen.py#L710-L753) —— 关键一行是 `return at::_ops::{f.func.name.unambiguous_name()}::call({exprs_str})`：它把公开 API 实现成「转调 `at::_ops::xxx::call`」，即真正干活的是 Dispatcher 入口（下一讲 [u3-l3](u3-l3-dispatchkey-and-dispatcher.md) 详述）。`unambiguous_name()` 把 `add.Tensor` 拼成 `add_Tensor`，作为 `at::_ops` 下的结构体名。

这些字符串最终被填进模板。以 `Function.h` 模板为例：

[aten/src/ATen/templates/Function.h:1-27](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/templates/Function.h#L1-L27) —— 模板里有 `${generated_comment}`、`${static_dispatch_ops_headers}`、`${operator_includes}`、`${function_definitions}` 四个占位符。`FileManager.substitute_with_template` 用 `string.Template` 把生成器产出的字符串填进去。

`FileManager` 的模板替换与「仅在变化时写盘」：

[torchgen/utils.py:144-195](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/utils.py#L144-L195) —— `substitute_with_template` 读模板、调 `env_callable()` 拿字典、`template.substitute(env)` 替换占位符；它还会自动注入 `generated_comment`（形如 `@generated by torchgen/gen.py from Function.h`），让每个生成文件都能追溯到自己来自哪个模板。

[torchgen/utils.py:131-141](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/utils.py#L131-L141) —— `_write_if_changed`：先尝试读旧文件，只有当新内容与旧内容不同才真正写盘。这非常重要——codegen 每次构建都跑，若每次都重写所有文件，会触发 CMake 把所有 `.cpp` 重新编译一遍。

构建侧如何调起 `gen.py`：

[cmake/Codegen.cmake:254-265](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/cmake/Codegen.cmake#L254-L265) —— `GEN_COMMAND` 就是 `python -m torchgen.gen --source-path .../aten/src/ATen --install_dir build/aten/src/ATen ...`，并根据 `USE_ROCM/USE_MPS/USE_XPU/USE_MTIA` 拼接对应开关。下方 [cmake/Codegen.cmake:271-336](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/cmake/Codegen.cmake#L271-L336) 用 `--output-dependencies` 先 dry-run 拿到产物清单，再 `include` 进 CMake，巧妙解决了「codegen 输出文件列表是动态的」这一问题。

#### 4.2.4 代码实践

**实践目标**：在 `gen.py` 中找到「生成函数式 / 方法式 C++ API」的调用点，并说出它们各自写入的目标文件名。

**操作步骤**（纯源码阅读，不运行）：

1. 打开 [torchgen/gen.py](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/gen.py)，定位 `gen_aggregated_headers`（[L1780](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/gen.py#L1780)）与 `gen_per_operator_headers`（[L1901](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/gen.py#L1901)）。
2. 在 `gen_aggregated_headers` 中找到三处 `cpu_fm.write(...)`：
   - `Operators.h` ← 由 `ComputeOperators(Target.DECLARATION, ...)` 生成 → 即 `at::_ops::xxx` 结构体声明。
   - `Functions.h` ← 由 `ComputeFunction()` 生成 → 即公开函数式 API `at::add`。
   - 在 `gen_headers`（[L2143](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/gen.py#L2143)）里 `core_fm.write("TensorBody.h", ...)` ← 由 `ComputeTensorMethod(...)` 生成 → 即 `Tensor::add` 等方法声明/定义。
3. 对比 `gen_per_operator_headers`：它把同样的内容**按 root_name 拆分**成 `ATen/ops/{name}_ops.h`、`ATen/ops/{name}.h`、`ATen/ops/{name}_native.h` 等小文件，目的是降低头文件依赖、加速增量编译（由 `--per-operator-headers` 开关控制）。

**需要观察的现象 / 预期结果**：你能列出至少三组「生成器类 → 目标文件」的对应关系，并解释 `Operators.h`（dispatcher 入口）与 `Functions.h`（公开 C++ API）为何是两个不同的文件——前者是「机器用的」低层入口，后者是「人类用的」便捷包装，前者被后者调用。

> 关于「Python 绑定」的精确边界：`gen.py` 本身只生成 **C++** 产物。真正把算子暴露成 Python 可调用对象（`torch.add`、`Tensor.add`）的绑定表（如 `python_torch_functions.cpp`、`python_variable_methods.cpp`）由 [tools/autograd/gen_python_functions.py](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/tools/autograd/gen_python_functions.py) 生成——但它同样调用 `torchgen.gen.parse_native_yaml`（见其 [第 62 行 import](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/tools/autograd/gen_python_functions.py#L62)）。也就是说，`torchgen` 的解析层是 C++ codegen 与 Python 绑定 codegen **共用**的底座。这与 [u2-l4](u2-l4-op-call-path-and-c-binding.md) 讲的 `_VariableFunctions` / `TensorBase` 两条 Python 入口是同源的。

#### 4.2.5 小练习与答案

**练习 1**：`gen.py` 的 `main()` 里，`gen_source_files` 和 `gen_headers` 谁先执行？顺序重要吗？

**参考答案**：从 [L3027-L3073](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/gen.py#L3027-L3073) 看，`sources` 在 `headers` 之前。但**顺序不重要**：两者都从同一份 `grouped_native_functions` 读数据、各自写各自的文件，互不依赖对方的产物（它们只是 CMake 编译时的相互 include 关系，不是 codegen 时的数据依赖）。

**练习 2**：为什么 `_write_if_changed` 对构建性能至关重要？

**参考答案**：codegen 在每次 CMake 配置/构建时都会被重新执行。如果每次都无条件覆写所有生成文件，文件 mtime 就会更新，CMake 会判定这些 `.cpp/.h` 需要重新编译，进而触发 `libtorch` 的大面积重编。`_write_if_changed` 保证「内容没变就不动文件」，是增量构建能正常工作的前提。

---

### 4.3 自动补全规则：native_function_generation.py

#### 4.3.1 概念说明

这是 codegen 最「聪明」的部分。问题是这样的：在 `native_functions.yaml` 里，一个算子通常以「三件套」出现——functional（`add`）、inplace（`add_`）、out（`add_out`）。但有些算子只写了其中一两个，比如只写了 inplace 变体。

为了后续的 functionalization、autograd、分组等流程能统一处理，codegen 希望**每个算子组都同时拥有 functional 与 out 变体**。`native_function_generation.py` 的职责就是：**根据已有变体，自动推导生成缺失的 functional / out 变体**，并把同一族算子聚合成 `NativeFunctionsGroup`。

它还会生成对应的「合成 kernel」——比如自动生成的 functional 变体，其 kernel 实现就是「调一次 inplace 变体（先 clone 可变输入），再把结果返回」。

#### 4.3.2 核心流程

整体分两步（在 `parse_native_yaml_struct` 中通过 `add_generated_native_functions` 调起）：

```
Step 1  pre_group_native_functions(rs)
        按 f.func.signature() 把「同族」算子归到同一个 dict，
        键是 SchemaKind。例：{functional: add.Tensor, inplace: add_.Tensor, out: add.out}

Step 2  对每个 group，检查缺失：
        - 若缺 out 变体且可生成 → generate_function(base, SchemaKind.out)
        - 若缺 functional 变体（且已有 out）→ generate_function(base, SchemaKind.functional)
        每个「生成」的新 NativeFunction 都被打上 "generated" tag，并附带
        一个 CompositeExplicitAutograd 的 BackendMetadata（指向合成 kernel 名）。
```

关键在于 `signature()`：它把 inplace/out/functional 三种变体**归一化成同一个核心签名**，这样它们才能被识别为「同族」。

签名归一化的直觉（非严格公式）：给定任意变体的 schema，其 signature 满足

\[
\mathrm{sig}(f).\mathrm{name}.\mathrm{inplace} = \text{False},\quad
\mathrm{sig}(f).\mathrm{arguments}.\mathrm{out} = \emptyset,\quad
\mathrm{sig}(f).\mathrm{arguments}.\mathrm{tensor\_options} = \mathrm{None}
\]

即「去掉尾下划线、去掉 out 参数、去掉 TensorOptions、去掉可变性注解」，只保留能区分算子语义的最小签名。

#### 4.3.3 源码精读

`pre_group_native_functions` 按 signature 分桶：

[torchgen/native_function_generation.py:112-123](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/native_function_generation.py#L112-L123) —— 遍历所有 `NativeFunction`，以 `f.func.signature()` 为 key 聚类；同一桶内若出现两个相同 SchemaKind 则报错（保证一族里每种变体至多一个）。

`generate_function` 是「从一个变体造出另一个变体」的核心：

[torchgen/native_function_generation.py:280-324](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/native_function_generation.py#L280-L324) —— 当 `k == SchemaKind.functional` 时，它调 `f.func.signature(keep_return_names=True)` 复用归一化逻辑，把可变参数翻成返回值；当 `k == SchemaKind.out` 时，根据原 schema 的 kind 分别调 `self_to_out_signature` / `mutable_to_out_signature` / `functional_to_out_signature`（见 [L136-L269](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/native_function_generation.py#L136-L269)）。

`add_generated_native_functions` 是总入口（注释说明它会**原地修改**两个入参）：

[torchgen/native_function_generation.py:395-516](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/native_function_generation.py#L395-L516) —— 它先 `pre_group`，再逐组判断：是否全是 `manual_cpp_binding`、是否是 view op、是否是纯 `CompositeImplicitAutograd`（这些情况下不生成变体）。否则尝试补出 out 变体（受 `needs_out` 与 `autogen` 列表约束）和 functional 变体。新造出的 `NativeFunction` 被 `append` 到 `rs`，其 BackendMetadata 通过 `BackendIndex.grow_index` 并入 `indices`。

合成 kernel：自动生成的 functional 变体如何实现？

[torchgen/native_function_generation.py:548-606](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/native_function_generation.py#L548-L606) —— `gen_composite_functional_kernel` 把生成的 functional 变体实现为「先把所有可变输入 clone 一份，调对应的 inplace/mutable 变体，再返回」。注意它只对带 `"generated"` tag 的算子生效，且要求组里必须有一个**非 generated** 的 inplace/mutable 变体作为真正的实现底座（避免无限递归）。

[torchgen/native_function_generation.py:611-666](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/native_function_generation.py#L611-L666) —— `gen_composite_out_kernel` 类似，把生成的 out 变体实现为「调 functional 变体拿结果，再 `resize_out_helper` + `copy_arg` 写进 out 参数」。

这些合成 kernel 最终被 `gen_source_files` 写进 `CompositeViewCopyKernels.cpp`（见 [torchgen/gen.py:2681-2730](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/gen.py#L2681-L2730) 里的 `GeneratedCompositeFunctional_Definitions` / `GeneratedCompositeOut_Definitions` 占位符）。

报错上下文：`with_native_function` 装饰器

[torchgen/context.py:45-79](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/context.py#L45-L79) —— `native_function_manager` 用 `context(lambda: f"in native_functions.yaml line {f.loc}:\n  {f.func}")` 包住生成逻辑，任何 AssertionError 都会带上「YAML 第几行、哪条 schema」的前缀，这正是前面 `LineLoader` 记录行号的价值。

#### 4.3.4 代码实践

**实践目标**：跟踪一个真实算子 `add`，看清它有哪些变体是 YAML 里手写的、哪些是 codegen 自动生成的。

**操作步骤**（在仓库根目录执行）：

```bash
python -c "
from torchgen.gen import parse_native_yaml
parsed = parse_native_yaml(
    'aten/src/ATen/native/native_functions.yaml',
    'aten/src/ATen/native/tags.yaml',
)
# 只看 root_name == 'add' 的算子
adds = [f for f in parsed.native_functions if f.root_name == 'add']
for f in adds:
    print(f'{str(f.func.name):28s} kind={str(f.func.kind()):22s} generated={(\"generated\" in f.tags)}')
"
```

**需要观察的现象**：

1. 你会看到多个 `add` 相关条目，包括 `add.Tensor`、`add.Scalar`、`add.out`、`add_.Tensor` 等。
2. 注意每个条目的 `kind` 列：`functional` / `inplace` / `out`。
3. `generated` 列为 `True` 的，就是 `native_function_generation.py` **自动补出来**的变体——它们不在 YAML 里手写，而是 codegen 推导生成的。

**预期结果**：`add.Tensor`、`add.out`、`add_.Tensor` 等都是 `generated=False`（手写）；你会看到某些 root_name（例如一些只有 inplace/mutable 变体的算子）会出现 `generated=True` 的 functional 或 out 变体。如果当前 `add` 全族都手写了，可以换一个 root_name（如某个带 mutable 变体的算子）再观察，或直接 grep `tags:.*generated` 的逻辑体会。

> 待本地验证：上述脚本需要在仓库根目录、且能 import `torchgen` 的环境下运行；不同 HEAD 下「哪些算子被自动生成」可能略有差异，以你本地实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `gen_composite_functional_kernel` 必须找一个**非 generated** 的 inplace/mutable 变体来调用？如果它去调另一个 generated 变体会怎样？

**参考答案**：因为 generated 的 functional 变体本身没有真实计算逻辑，它只是「转调底座」的壳。如果它调另一个 generated 变体，就会形成「壳调壳」的无限递归（或至少是空转）。注释里也明确：「generated functional kernels are always implemented in terms of non-generated kernels」。

**练习 2**：`pre_group_native_functions` 用 `f.func.signature()` 作为分桶 key。给定 `add.Tensor`、`add_.Tensor`、`add.out` 三者，它们的 `signature()` 是否相同？为什么这很关键？

**参考答案**：相同。`signature()` 会剥掉 inplace 下划线、去掉 out 参数与可变性注解，三者归一化后都得到 `add(Tensor self, Tensor other, *, Scalar alpha=1) -> Tensor`。这很关键——只有三者被识别为「同族」，`add_generated_native_functions` 才能把它们装进同一个 `NativeFunctionsGroup`，进而统一处理 functionalization、autograd、合成 kernel 生成。

## 5. 综合实践

把本讲三个模块串起来，完成一次「端到端追踪」：

**任务**：选定一个结构化算子 `add`（schema 见 [native_functions.yaml:542](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/native_functions.yaml#L542) 的 `add.Tensor` 与 [L565](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/native_functions.yaml#L565) 的 `add.out`），画出它从 YAML 到生成文件的全链路图。

**要求**：

1. 用 `parse_native_yaml` 解析后，打印 `add` 族所有 `NativeFunction` 与它们归属的 `NativeFunctionsGroup`（用 `get_grouped_native_functions`，见 [torchgen/gen.py:1467](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torchgen/gen.py#L1467)），标出哪些是 generated。
2. 找到 `add` 族会触发的生成器：`ComputeFunction`（→ `Functions.h` / `ops/add.h`）、`ComputeOperators`（→ `Operators.h` / `ops/add_ops.h`）、`ComputeTensorMethod`（→ `TensorBody.h`），以及 structured 特有的 `compute_meta_function_declaration`（→ `ops/add_meta.h`）。
3. 写一段文字说明：为什么 `add.Tensor` 标了 `structured_delegate: add.out`（见 [native_functions.yaml:544](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/aten/src/ATen/native/native_functions.yaml#L544)），而 `add.out` 标了 `structured: True`——这与本讲 4.3 的「自动补全」有何关系？（提示：structured 族以 out 变体为「真身」，functional/inplace 通过 delegate 复用其形状推导逻辑。）

**预期产出**：一张含「YAML 条目 → 数据模型对象 → 生成器 → 目标文件」四列的表格，外加一段对 structured 机制的解读。

## 6. 本讲小结

- **代码生成是 PyTorch 管理数千算子的根本策略**：人类只维护 `native_functions.yaml` 这一份单一事实来源，机器保证所有 C++/Python 派生产物彼此一致。
- **`model.py` 是语义层**：`NativeFunction` 与 `FunctionSchema` 用 frozen dataclass + 「无损往返」断言，把 YAML 文本忠实地建模成强类型对象；dispatch 信息被外置到 `BackendIndex` 以支持后端扩展。
- **`gen.py` 是总调度**：`main()` 分 `headers` / `sources` / `declarations_yaml` 三阶段；每阶段把一组「生成器（吃 NativeFunction 吐字符串）」套到算子列表上，再经 `FileManager` 的模板替换 + `_write_if_changed` 落盘。
- **柯里化生成器 + `mapMaybe`** 是 codegen 的主导风格：`ComputeFunction` / `ComputeOperators` / `ComputeTensorMethod` 等都是「配置 → (NativeFunction → 字符串)」的柯里化函数。
- **`native_function_generation.py` 实现「自动补全」**：用 `signature()` 归一化识别同族算子，自动生成缺失的 functional/out 变体并配上合成 kernel，让下游（functionalization / autograd）总能假设「每族都有完整三件套」。
- **报错可定位**：`LineLoader` 记录 YAML 行号，`with_native_function` 在出错时把它带进上下文，是 codegen 可维护性的关键基础设施。

## 7. 下一步学习建议

本讲止步于「YAML → 生成文件」。建议接下来：

1. **[u3-l3 DispatchKey 与 Dispatcher 分发机制](u3-l3-dispatchkey-and-dispatcher.md)**：本讲反复出现的 `at::_ops::xxx::call`、`Register{DispatchKey}.cpp`、`BackendIndex` 都指向 Dispatcher。下一讲讲清一次算子调用在运行时如何按 DispatchKey 查表跳转到具体 kernel。
2. **读一个生成产物**：构建一次 PyTorch 后，打开 `build/aten/src/ATen/ops/add_ops.h` 与 `add.h`，对照本讲的 `ComputeOperators` / `ComputeFunction` 源码，验证「生成器输出的字符串」与「磁盘上的 C++」一致。
3. **读 `tools/autograd/gen_python_functions.py`**：理解 Python 绑定表是如何复用 `torchgen.gen.parse_native_yaml` 的，把本讲的 codegen 与 [u2-l4](u2-l4-op-call-path-and-c-binding.md) 的 Python 调用路径接起来。
4. **进阶**：阅读 `torchgen/dest/` 与 `torchgen/api/`（如 `api/cpp.py`、`api/dispatcher.py`），理解「同一 schema 如何被翻译成三种不同 C++ API 类型」——这是 `gen.py` 顶部注释提到的「heavy lifting」所在。
