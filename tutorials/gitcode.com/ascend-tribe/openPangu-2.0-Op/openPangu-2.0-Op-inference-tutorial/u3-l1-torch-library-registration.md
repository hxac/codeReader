# csrc 适配层：TORCH_LIBRARY 注册算子签名

## 1. 本讲目标

第 1 单元（u1-l4）我们已经知道：`omni_custom_ops` wheel 包里的 C++ 扩展 `custom_ops_lib` 被 import 时，会把算子注册进 `torch.ops.custom` 命名空间，从而可以用 `torch.ops.custom.npu_xxx(...)` 调用。本讲打开这个 `.so` 的源码，搞清楚「注册」这件事本身是怎么写的：

1. 读懂 `csrc_base/ops_def_registration.cpp` 中 `TORCH_LIBRARY_FRAGMENT(custom, m)` 块里几十条 `m.def(...)` 的算子签名语法：`Tensor` / `int` / `str` / 可选参数 `Tensor?`、默认值、`*` 关键字参数分隔符、`-> (Tensor, Tensor)` 多返回值等。
2. 理解每个算子 `csrc/` 目录下 `TORCH_LIBRARY_IMPL` 的两个实现键：`PrivateUse1`（NPU 真实计算）与 `Meta`（只推形状、不计算）的区别与分工。
3. 掌握新增一个算子时「先定义、后实现」的两步注册步骤，以及新文件放在哪里才能被 `setup.py` 自动收集编译。

学完本讲，你应该能独立为一个新算子补上 PyTorch 侧的注册代码（暂不涉及 `EXEC_NPU_CMD_V1` 内部机制，那是下一讲 u3-l2 的内容）。

## 2. 前置知识

### 2.1 PyTorch 算子与 torch.ops

PyTorch 中「算子」（op）是一个带类型签名的操作声明，全部登记在 PyTorch 的**算子库**（library）里。Python 侧通过 `torch.ops.<库名>.<算子名>` 访问，例如 PyTorch 内置的 `torch.ops.aten.add`。本仓库把所有自定义算子放进名为 `custom` 的库，于是调用入口就是 `torch.ops.custom.npu_xxx(...)`。

### 2.2 TorchDispatcher 与调度键（Dispatch Key）

当你调用 `torch.ops.custom.npu_xxx(x)` 时，PyTorch 并不直接执行某个 C++ 函数，而是先经过**分发器**（dispatcher）：分发器看输入张量所在的「设备 / 模式」，选一个**调度键**，再执行挂在这个键下的实现函数。可以把分发器理解为电话总机：同一个号码（算子名），根据来电区域（张量设备），转接到不同的分机（实现函数）。

本讲涉及两个调度键：

| 调度键 | 含义 | 本仓库中的用途 |
| --- | --- | --- |
| `PrivateUse1` | PyTorch 预留给第三方后端的设备键，`torch_npu` 用它代表 NPU 设备 | 真实计算实现：把张量转成 acl 接口参数，下发到 NPU |
| `Meta` | 虚构的 "meta 设备"，张量只有形状和 dtype、没有数据 | 只推导输出形状，不做任何计算 |

经验法则：**输入张量在哪个设备，就走哪个键的实现**。`x.npu()` 的张量走 `PrivateUse1`；`torch.empty(..., device='meta')` 的张量走 `Meta`。图模式（torchair/torch.compile）在编译期做 shape 推导时，也会调用 Meta 实现。

### 2.3 算子签名（schema）

每个算子必须先有一个**签名**——一个描述「参数叫什么、什么类型、有没有默认值、返回什么」的字符串，例如：

```
npu_lower_triangular_inverse(Tensor x) -> Tensor
```

签名是 PyTorch 调度体系的「合同」：分发器按签名做参数类型检查与转换，Python 关键字传参、默认值填充也都依据它。**没有签名的算子无法被调用**——这就是「先定义、后实现」的原因。

### 2.4 与前几讲的衔接

- u1-l4 讲过 `import omni_custom_ops` 是挂载算子的开关。本讲解释其内部机制：import 触发 `custom_ops_lib.so` 加载，`.so` 里的静态注册代码（`TORCH_LIBRARY_FRAGMENT` / `TORCH_LIBRARY_IMPL` 宏）在加载瞬间向 PyTorch 算子库登记一切。
- u2 系列讲过 AscendC 算子三层结构（op_api/op_host/op_kernel）。本讲的 csrc 层在三层结构**之外**、更靠近用户：csrc 收到 `at::Tensor` 后，通过 `EXEC_NPU_CMD_V1(aclnnXxx, ...)` 借道 op_api 层的 aclnn 接口完成真实计算。也就是说 csrc 只做「翻译 + 转发」，不写计算逻辑。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp` | **唯一的签名定义文件**：所有算子的 `m.def` 集中在此 |
| `ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp` | 最小样本：单输入单输出算子的 NPU + Meta 双实现 |
| `ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/index/ai_infra_scatter_block_update/csrc/npu_ai_infra_scatter_block_update.cpp` | 原地（in-place）算子样本：`Tensor(a!)` 与 `-> ()` 语法 |
| `ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/csrc/npu_ai_infra_mhc_sandwich_norm_post_preonly.cpp` | 复杂样本：可选参数、float 参数、多返回值、共享 shape 推导函数 |
| `ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/posembedding/ai_infra_rotary_position_embedding/csrc/npu_ai_infra_rotary_mul.cpp` | `str` 参数在实现侧的写法（`c10::string_view`） |
| `ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/sparse_flash_attention_gqa/csrc/npu_sparse_flash_attention_gqa.cpp` | `SymInt[]?` 参数与 `.out` 变体的实现侧写法 |
| `ascendc/torch_ops_extension/setup.py` | 打包脚本：两条 glob 规则自动收集 csrc 源文件 |
| `ascendc/torch_ops_extension/omni_custom_ops/__init__.py` | import 副作用加载 `.so` 并把算子镜像到 `torch_npu` |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**4.1 TORCH_LIBRARY_FRAGMENT（先定义）**、**4.2 m.def 签名语法（定义写什么）**、**4.3 TORCH_LIBRARY_IMPL 与 Meta 实现（后实现）**。

### 4.1 TORCH_LIBRARY_FRAGMENT：先把算子「定义」出来

#### 4.1.1 概念说明

`TORCH_LIBRARY(名字, m)` 宏的作用是：创建（或取得）一个名为 `custom` 的算子库，并在宏的作用域内通过 `m` 往里添加内容；这段注册代码会被编译进 `.so`，在动态库被加载时自动执行。

`TORCH_LIBRARY_FRAGMENT(名字, m)` 是它的「碎片」版本：允许**多个文件各自声明同名库的碎片，共同往一个库里添内容**。为什么必须用 FRAGMENT？因为：

- 本仓库有 20 多个算子，每个算子一个 csrc 文件；如果两个文件都写完整的 `TORCH_LIBRARY(custom, m)`（非碎片版），等于声明「这个库归我管」两次，加载时会报重复定义错误。
- 碎片版只承诺「我往 custom 库里贡献几条」，库本身的存在与否由 PyTorch 统一管理。

本仓库的分工非常清晰：

- **`csrc_base/ops_def_registration.cpp`**：唯一写 `m.def`（签名定义）的地方，**不写任何实现**。
- **各算子 `csrc/*.cpp`**：只写 `m.impl`（实现注册），不写 `m.def`。

一个算子想被 Python 调到，两处缺一不可：先在 `ops_def_registration.cpp` 有签名，再在算子自己的 csrc 文件里把实现挂到调度键上。

#### 4.1.2 核心流程

从 Python 调用到进入 C++ 实现的完整链路：

```text
import omni_custom_ops                       # Python
    └─ from . import custom_ops_lib          # 加载 .so（__init__.py 第 20 行）
         └─ .so 内静态注册代码执行
              ├─ TORCH_LIBRARY_FRAGMENT(custom, m) 中的 m.def(...)
              │     → custom 库获得算子签名（此时还没有实现！）
              └─ 各 csrc 文件的 TORCH_LIBRARY_IMPL(custom, 键, m) 中的 m.impl(...)
                    → 把 C++ 函数挂到 (算子名, 调度键) 下
torch.ops.custom.npu_xxx(x)                  # 用户调用
    └─ 分发器查签名 → 校验/补默认参数 → 按张量设备选键
         ├─ NPU 张量  → PrivateUse1 实现（真实计算）
         └─ meta 张量 → Meta 实现（只推形状）
```

注意时序：`m.def` 与 `m.impl` 都在 `.so` 加载的瞬间完成，谁先谁后取决于链接顺序，但 PyTorch 允许「先 def 后 impl」也允许「先 impl 后 def」（impl 可以在 def 之前注册，只要加载完成时两者都齐了即可）。**只有 def 没有 impl** 的算子，调用时会抛 "NotImplementedError" 类错误；**只有 impl 没有 def** 的算子则根本不存在。

#### 4.1.3 源码精读

先看签名定义文件的开头，仓库作者用中文注释直接点明了这一层的规矩：

- [ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp:14-16](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L14-L16)：注释写着「在custom命名空间里注册后续的XXX算子，每次新增自定义aten ir都需先增加定义 / step1, 为新增自定义算子添加定义」，随后是本文件唯一的 `TORCH_LIBRARY_FRAGMENT(custom, m)` 块开始。**「step1」这个词就是「先定义后实现」步骤的出处**——step2（实现）在各算子 csrc 文件里。
- [ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp:166-168](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L166-L168)：文件末尾是一个**空的** `PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {}`，注释「通过pybind将c++接口和python接口绑定，这里绑定的是接口不是算子」。这行容易让初学者困惑：传统 PyTorch 扩展靠 pybind11 逐个暴露函数，而本仓库**不走 pybind 暴露算子**，靠的是 `TORCH_LIBRARY_FRAGMENT` + 分发器；pybind 模块体留空，只是扩展 `.so` 的形式要求。

再对照任意一个实现文件，验证「实现侧不再出现 def」：

- [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp:37-43](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp#L37-L43)：文件末尾两个 `TORCH_LIBRARY_IMPL(custom, PrivateUse1, m)` 与 `TORCH_LIBRARY_IMPL(custom, Meta, m)` 块，只有 `m.impl` 没有 `m.def`；其上方的注释还特意标注「Schema is defined in csrc_base/ops_def_registration.cpp」（见同目录 mhc 文件的同款注释 [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/csrc/npu_ai_infra_mhc_sandwich_norm_post_preonly.cpp:133](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/csrc/npu_ai_infra_mhc_sandwich_norm_post_preonly.cpp#L133)）。

最后看加载入口：

- [ascendc/torch_ops_extension/omni_custom_ops/__init__.py:17-20](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/__init__.py#L17-L20)：先 `import torch`、`import torch_npu`（确保第三方后端键已就绪），再 `from . import custom_ops_lib`——这一行触发 `.so` 加载，全部注册代码在这一刻执行。
- [ascendc/torch_ops_extension/omni_custom_ops/__init__.py:31-41](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/__init__.py#L31-L41)：加载完成后，遍历 `torch.ops.custom` 命名空间里的算子，逐个 `setattr(torch_npu, op_name, custom_op_func)`——这就是 u1-l4 讲过的「`torch_npu.npu_xxx` 与 `torch.ops.custom.npu_xxx` 等价」的实现处。

#### 4.1.4 代码实践

**实践目标**：不改任何源码，亲眼确认「签名定义」与「实现注册」是两件独立的事，并观察签名对象。

**操作步骤**（需要已按 u1-l4 装好 wheel 包的环境；无环境则跳过执行、只做步骤 3 的静态阅读，标注「待本地验证」）：

1. 写一个最小脚本 `inspect_ops.py`（示例代码）：

   ```python
   import torch
   import torch_npu
   import omni_custom_ops          # 触发注册

   op = torch.ops.custom.npu_lower_triangular_inverse
   print(op.default._schema)        # 打印签名对象
   print([k for k in dir(torch.ops.custom) if not k.startswith('_')])
   ```

2. 运行 `python inspect_ops.py`，观察输出。

**需要观察的现象**：

- 打印出的 schema 字符串与 `ops_def_registration.cpp` 第 53 行 `npu_lower_triangular_inverse(Tensor x) -> Tensor` 逐字对应；
- 命名空间列表里能同时看到 `npu_lower_triangular_inverse`、`npu_ai_infra_scatter_block_update`、`npu_ai_infra_scatter_block_update_`（带下划线的原地版）等所有 `m.def` 过的算子。

**预期结果**：签名来自 `ops_def_registration.cpp`，证明 def 与 impl 分离存放但共同生效。

#### 4.1.5 小练习与答案

**练习 1**：如果把某个算子 csrc 文件里的 `TORCH_LIBRARY_IMPL(custom, PrivateUse1, m)` 整块删掉（def 保留），import 还能成功吗？调用会发生什么？

**答案**：import 仍能成功——`m.def` 在 `ops_def_registration.cpp` 里，签名照常注册。但该算子在 `PrivateUse1` 键下没有实现，用 NPU 张量调用 `torch.ops.custom.npu_xxx(...)` 会在运行期抛出 "NotImplementedError: Could not run 'custom::npu_xxx' with arguments from the 'PrivateUse1' backend" 一类错误（具体文案随 PyTorch 版本变化，待本地验证）。

**练习 2**：为什么 `ops_def_registration.cpp` 用 `TORCH_LIBRARY_FRAGMENT` 而不是 `TORCH_LIBRARY`？

**答案**：`TORCH_LIBRARY` 声明库的「所有权」，同名库只能被完整声明一次；`FRAGMENT` 允许多个文件向同一个 `custom` 库追加内容。本仓库 20 多个 csrc 文件 + 1 个 def 文件都要操作 `custom` 库，若都用非碎片版会重复声明冲突。

**练习 3**：`PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {}` 的模块体为什么是空的？

**答案**：本仓库不通过 pybind11 逐函数暴露接口，而是通过 `TORCH_LIBRARY_FRAGMENT` 把算子登记进 PyTorch 分发器，Python 侧经 `torch.ops.custom.*` 访问；pybind 模块定义只是构建一个合法 torch 扩展 `.so` 的形式要求，模块体留空即可（文件内注释：「绑定的是接口不是算子」）。

### 4.2 m.def 签名语法：一张合同怎么写

#### 4.2.1 概念说明

`m.def("名字(参数列表) -> 返回类型")` 中的字符串是 PyTorch 的 schema DSL。它要回答五个问题：

1. 算子叫什么（`npu_lower_triangular_inverse`）；
2. 每个参数叫什么、什么类型、有没有默认值（`int pre_tokens=2147483647`）；
3. 哪些参数必须按关键字传（`*` 之后的参数）；
4. 参数是否可变（原地修改标记 `Tensor(a!)`）；
5. 返回什么（`Tensor`、多元组 `(Tensor, Tensor)`、或无返回 `()`）。

签名是 host 侧的纯声明——写错了类型或漏了参数，编译期不一定报错，而是在**调用时**由分发器按 schema 校验并报错。所以读懂签名语法，是排查「调用报参数错」的基本功。

#### 4.2.2 核心流程与语法对照

先看仓库里从简到繁的三个真实签名，再给类型对照表。

最简（单输入单输出）：

```cpp
m.def("npu_lower_triangular_inverse(Tensor x) -> Tensor");
```

原地版本（带 `Tensor(a!)` 可变标记、无返回值）：

```cpp
m.def("npu_ai_infra_scatter_block_update(Tensor input, Tensor indices, Tensor update) -> Tensor");
m.def("npu_ai_infra_scatter_block_update_(Tensor(a!) input, Tensor indices, Tensor update) -> ()");
```

最复杂（`npu_fused_infer_attention_sink`，节选）：必选参数在前，`*` 之后全部变成「只能按关键字传」的可选参数，并带各种默认值，返回二元组：

```cpp
m.def("npu_fused_infer_attention_sink(Tensor query, Tensor key, Tensor value, *, Tensor? query_rope=None, "
      "... int num_query_heads=1, float softmax_scale=1.0, str input_layout='TND', "
      "bool return_softmax_lse=False, int? query_dtype=None, ...) -> (Tensor, Tensor)");
```

schema 类型 → 实现函数 C++ 形参类型对照表（每一行都在本仓库源码中验证过）：

| schema 写法 | 含义 | 实现侧 C++ 类型 | 仓库出处 |
| --- | --- | --- | --- |
| `Tensor` | 必选张量 | `const at::Tensor&` | lower_triangular_inverse.cpp:20 |
| `Tensor?` | 可选张量（默认 `None`） | `const c10::optional<at::Tensor>&` | mhc csrc:89（`gamma_2`） |
| `Tensor(a!)` | 可变（原地写入）张量 | `at::Tensor&`（非 const） | scatter csrc:28（`input`） |
| `int` | 标量整数 | `int64_t` | gqa csrc:468 |
| `int?` | 可选整数 | `c10::optional<int64_t>` | fia sink csrc:265（`query_dtype`） |
| `float` | 标量浮点 | `double` | mhc csrc:89（`norm_eps`） |
| `bool` | 布尔 | `bool` | mhc csrc:103 |
| `str` | 字符串 | `c10::string_view` | rotary_mul csrc:67 |
| `SymInt[]?` | 可选符号整数列表 | `c10::OptionalArrayRef<c10::SymInt>` | gqa csrc:502 |
| `-> Tensor` | 单返回 | `at::Tensor` | lower_triangular_inverse.cpp:20 |
| `-> (Tensor, Tensor)` | 多返回 | `std::tuple<at::Tensor, at::Tensor>` | gqa csrc:464 |
| `-> ()` | 无返回 | `void` | scatter csrc:28 |

（表中「仓库出处」指该类型的实现函数形参所在文件与行号，见 4.2.3 的永久链接。）

其他语法要点：

- **`*` 分隔符**：`*` 之后的参数在 Python 侧只能用关键字传（`f(q, k, v, num_query_heads=8)`），防止几十个可选参数的位置歧义。
- **默认值**：写在类型后面（`str input_layout='TND'`），调用方不传就取默认值。
- **`Tensor(a!)` 中的注释标记**：`a!` 表示这个参数会被原地修改（annotation），`npu_ai_infra_scatter_block_update_` 的 input 就带它；调用后原张量内容被更新。带 `!` 的参数必须以非 const 引用接收。
- **算子名后缀与变体**：
  - 名字末尾的 `_`（如 `..._update_`）是 PyTorch 惯例的「原地版」算子名，与 `Tensor(a!)`、`-> ()` 配套；
  - `名字.out` / `名字.tensor` 是同一算子的**命名变体**（schema 重载），如 `npu_sparse_flash_attention_gqa.out` 允许调用方传入预分配的输出张量（`Tensor[] out`）与 workspace；
  - 名字以 `_` 开头的算子（如 `_npu_fused_infer_attention_sink_metadata`）表示「内部算子」，Python 侧 `dir()` 会把它当私有成员跳过（见 `__init__.py` 的 `op_name.startswith('_')` 过滤）。

#### 4.2.3 源码精读

- [ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp:53](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L53)：最简签名 `npu_lower_triangular_inverse(Tensor x) -> Tensor`——本讲反复使用的标本。
- [ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp:107-108](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L107-L108)：scatter 的两个版本并排——非原地版返回新 Tensor；原地版 `npu_ai_infra_scatter_block_update_` 把 `input` 标成 `Tensor(a!)` 且返回 `()`。**同一底层 aclnn 接口（aclnnAiInfraScatterBlockUpdate），在 Python 侧暴露成两种调用姿势**，对应关系见 4.3.3。
- [ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp:18-35](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L18-L35)：旗舰算子 `npu_fused_infer_attention_sink` 的完整签名——第 4 个参数后出现 `*`，其后约 40 个参数全部是带默认值的可选参数（`Tensor?`、`int`、`float`、`str`、`bool`、`int?` 混排），返回 `(Tensor, Tensor)`。
- [ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp:95-106](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L95-L106)：MHC 算子两个版本的签名——v1 里 `Tensor? gamma_2=None` 夹在必选参数之间、`float norm_eps=1e-6` 带浮点默认值；v2 把 `gamma_2` 挪到 `*` 之后并新增 `bool return_h_in_f32=False`，返回值从二元组变三元组 `(Tensor, Tensor, Tensor)`。**同名加 `_v2` 后缀扩接口**是仓库演化接口的常用手法。
- [ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp:60-65](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L60-L65)：`npu_sparse_flash_attention_gqa.out` 变体——签名尾部多了 `Tensor? workspace=None, Tensor[] out`，其中 `Tensor[] out` 是**非可选**的张量列表，调用方必须传入接收结果的容器。
- [ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp:113-124](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L113-L124)：`npu_ai_infra_kv_rmsnorm_rope_cache` 的 v1/v2 对照——v2 直接把 `k_cache`、`ckv_cache` 声明为 `Tensor(a!)`、`Tensor(b!)`（两个不同的可变标记），返回值也从 4 元组缩到 2 元组（缓存改为原地写入后不必再返回）。

实现侧类型映射的验证点：

- [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/posembedding/ai_infra_rotary_position_embedding/csrc/npu_ai_infra_rotary_mul.cpp:67-68](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/posembedding/ai_infra_rotary_position_embedding/csrc/npu_ai_infra_rotary_mul.cpp#L67-L68)：`str rotary_mode` 映射为 `c10::string_view`、`Tensor? rotate=None` 映射为 `const c10::optional<at::Tensor>&`。
- [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/sparse_flash_attention_gqa/csrc/npu_sparse_flash_attention_gqa.cpp:502-503](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/sparse_flash_attention_gqa/csrc/npu_sparse_flash_attention_gqa.cpp#L502-L503)：`SymInt[]? actual_seq_lengths_query` 映射为 `c10::OptionalArrayRef<c10::SymInt>`。
- [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/fused_infer_attention_sink/csrc/npu_fused_infer_attention_sink.cpp:265-270](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/fused_infer_attention_sink/csrc/npu_fused_infer_attention_sink.cpp#L265-L270)：一排 `c10::optional<int64_t>` 接住签名里的 `int? ..._dtype` 参数。

#### 4.2.4 代码实践

**实践目标**：训练「从签名反推 Python 调用方式」的能力，全部为纯阅读实践，无需环境。

**操作步骤**：

1. 打开 [ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp:95-106](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L95-L106)，只看 `npu_ai_infra_mhc_sandwich_norm_post_preonly` 的签名，在纸上写出一段合法的 Python 调用（伪代码即可）：传必选参数、按关键字传 `gamma_2` 与 `norm_eps`、其余取默认值，并写出返回值解包语句。
2. 再对照同文件 [L18-35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L18-L35) 的 `npu_fused_infer_attention_sink`，回答：`pre_tokens`（int 型带默认值）能否作为第 4 个位置参数传入？
3. 把你的答案与 `ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/tests/st/test_ai_infra_scatter_block_update.py` 或 u1-l4 提过的 example 脚本中的真实调用对照（多输出解包、关键字传参的写法）。

**需要观察的现象 / 预期结果**：

1. 第 1 步参考答案（示例代码）：

   ```python
   h_in_prime, x_2_out = torch.ops.custom.npu_ai_infra_mhc_sandwich_norm_post_preonly(
       h_out, residual, h_post, h_res, phi, alpha_0, bias, gamma_0, gamma_1,
       gamma_2=g2, norm_eps=1e-5)   # hc_eps 取默认 1e-6
   ```

   返回值必须按二元组解包，因为签名是 `-> (Tensor, Tensor)`。
2. 第 2 步答案：**不能**。`pre_tokens` 位于 `*` 之后，只能按关键字传 `pre_tokens=...`；作为位置参数传入会报 "argument for parameter" 类调度错误。
3. 与真实脚本对照时确认：多返回值算子都按元组解包，可选参数都按关键字传——与 schema 约束一致。

#### 4.2.5 小练习与答案

**练习 1**：`Tensor? query_rope=None` 与 `Tensor query_rope` 在调用和实现两侧各有什么区别？

**答案**：调用侧：前者可省略（默认 `None`），后者必须传张量。实现侧：前者形参是 `const c10::optional<at::Tensor>&`，用前需判 `has_value()`（或直接透传给 aclnn 层判空）；后者是 `const at::Tensor&`，可直接解引用。

**练习 2**：`npu_ai_infra_scatter_block_update_` 的 `Tensor(a!)` 标记，如果实现函数把形参写成 `const at::Tensor &input` 会怎样？

**答案**：编译失败（或语义错误）。`a!` 声明该参数要被原地修改，实现必须能拿到可变引用，所以形参须为 `at::Tensor&`（非 const）——仓库实现正是这么写的（scatter csrc 第 28 行）。const 引用无法完成写回。

**练习 3**：`npu_sparse_flash_attention_gqa`、`..._gqa.out`、`..._gqa.tensor` 三个 def 是什么关系？为什么 `.out` 变体的 `Tensor[] out` 不给默认值？

**答案**：是同一算子名的三个 schema 变体（重载），分别服务「常规调用」「调用方自带输出张量与 workspace」「只收 Tensor 类型序列长度参数」的场景。`.out` 的意义就是让调用方传入输出容器，若再给默认值就失去了变体存在的目的；而 `workspace` 允许 `None`，故写作 `Tensor? workspace=None`。

### 4.3 TORCH_LIBRARY_IMPL：PrivateUse1 与 Meta 两个实现键

#### 4.3.1 概念说明

签名定义好的算子只是「空壳」，`TORCH_LIBRARY_IMPL(custom, 键, m)` 负责把 C++ 函数挂到壳下的具体键上。本仓库每个算子固定挂两个键：

- **`PrivateUse1`：NPU 真算**。torch_npu 用 `PrivateUse1` 这个预留设备键代表 NPU。挂在它下面的实现函数是真正干活的：分配输出张量、通过 `EXEC_NPU_CMD_V1(aclnnXxx, 输入..., 输出...)` 把 `at::Tensor` 转成交付给 aclnn 接口的参数并异步下发（EXEC 宏的内部机制是下一讲 u3-l2 的主题，本讲只需把它当成「调 aclnn 的固定写法」）。
- **`Meta`：形状推导**。挂在 Meta 下的函数**只构造输出张量的形状，不做计算、不碰 NPU**。它服务的场景是：`device='meta'` 张量上的试运行、`torch.compile`/torchair 图模式编译期的 shape 推导。Meta 实现通常是 NPU 实现的「去掉 EXEC 那一行」。

一个值得注意的写法差异：Meta 实现常用 `at::empty_symint` + `sym_size`（符号形状）而非 `at::empty_like`——因为图编译期具体数值可能未知，符号形状能在不知道确切大小时完成推导。

#### 4.3.2 核心流程

以 `torch.ops.custom.npu_lower_triangular_inverse(x)` 为例：

```text
分发器收到调用
  ├─ 查签名：custom::npu_lower_triangular_inverse(Tensor x) -> Tensor，校验 x
  ├─ 看 x.device：
  │    ├─ npu  → 选 PrivateUse1 键 → custom::npu_lower_triangular_inverse (lower_triangular_inverse.cpp:20)
  │    │         ① TORCH_CHECK 检查维度
  │    │         ② at::empty_like 分配输出
  │    │         ③ EXEC_NPU_CMD_V1(aclnnAiInfraLowerTriangularInverse, x, result) 下发计算
  │    │         ④ return result
  │    └─ meta → 选 Meta 键 → custom::npu_lower_triangular_inverse_meta (同文件:28)
  │              ①②④ 同上，但没有 ③——不执行任何计算
  └─ 其他设备（如 cpu）→ 无实现，报错
```

两个键的实现共享「构造输出」的代码是仓库的常见优化：MHC 算子把 shape 推导抽成 `construct_output_tensors`，NPU 实现与 Meta 实现都调它，只是 NPU 版多一步 EXEC。

#### 4.3.3 源码精读

**样本一：单输入单输出**（lower_triangular_inverse）：

- [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp:20-26](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp#L20-L26)：PrivateUse1 实现的函数体——`TORCH_CHECK` 检查 5 维输入、`at::empty_like(x)` 按输入同形状分配输出、`EXEC_NPU_CMD_V1(aclnnAiInfraLowerTriangularInverse, x, result)` 调 aclnn 接口（第一个宏参是 op_api 层函数名，之后按 aclnn 参数顺序列实参）、返回结果。这就是一个标准 NPU 实现的全部四步。
- [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp:28-33](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp#L28-L33)：对应 Meta 实现——同样检查、同样 `empty_like`，**唯独没有 EXEC 行**，直接返回空壳张量。对照读这两段，就能记住 Meta 的本质：「NPU 实现减去计算」。
- [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp:37-43](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp#L37-L43)：两块 `TORCH_LIBRARY_IMPL` 注册——同一算子名分别挂到 `PrivateUse1` 与 `Meta`，`m.impl` 的第一个参数是算子名字符串（必须与 `m.def` 的名字一致），第二个是函数指针。

**样本二：原地 + 非原地双版本**（scatter_block_update）：

- [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/index/ai_infra_scatter_block_update/csrc/npu_ai_infra_scatter_block_update.cpp:19-32](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/index/ai_infra_scatter_block_update/csrc/npu_ai_infra_scatter_block_update.cpp#L19-L32)：非原地版先 `input.clone()` 再在副本上 EXEC，返回副本（保证函数式语义，调用方原张量不变）；原地版 `void` 返回、形参 `at::Tensor &input` 非 const，直接在原张量上 EXEC。**两个版本调的是同一个 aclnn 接口** `aclnnAiInfraScatterBlockUpdate`，差别只在 csrc 层要不要 clone——又一次印证 u1-l3 的结论：op_api 层的本算子本来就是原地写，非原地语义是 csrc 用 clone 包出来的。
- [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/index/ai_infra_scatter_block_update/csrc/npu_ai_infra_scatter_block_update.cpp:35-46](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/index/ai_infra_scatter_block_update/csrc/npu_ai_infra_scatter_block_update.cpp#L35-L46)：两个 Meta 实现——非原地版用 `std::vector<c10::SymInt>` + `at::empty_symint` 构造形状为 `{input.size(0), input.size(1), input.size(2)}` 的输出（注意它取前三维，说明输出形状的推导逻辑写在 csrc 层）；原地版 Meta 是**空函数体**——原地算子不产生新张量，没有 shape 可推。
- [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/index/ai_infra_scatter_block_update/csrc/npu_ai_infra_scatter_block_update.cpp:50-58](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/index/ai_infra_scatter_block_update/csrc/npu_ai_infra_scatter_block_update.cpp#L50-L58)：两块 IMPL 各注册两条 `m.impl`（普通版与 `_` 版）——一个键下可以一次挂多个算子。

**样本三：多返回值与可选输出**（mhc_sandwich_norm_post_preonly）：

- [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/csrc/npu_ai_infra_mhc_sandwich_norm_post_preonly.cpp:29-51](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/csrc/npu_ai_infra_mhc_sandwich_norm_post_preonly.cpp#L29-L51)：共享的 `construct_output_tensors`——按输入是 2 维还是 3 维，用 `sym_size`/`empty_symint` 推出两个输出张量的形状。NPU 与 Meta 实现都复用它。
- [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/csrc/npu_ai_infra_mhc_sandwich_norm_post_preonly.cpp:86-97](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/csrc/npu_ai_infra_mhc_sandwich_norm_post_preonly.cpp#L86-L97)：v1 的 PrivateUse1 实现——先构输出，再 `EXEC_NPU_CMD_V1(aclnnAiInfraMhcSandwichNormPostPreonly, 全部输入..., gamma_2, norm_eps, hc_eps, h_in_prime, x_2_out)`。注意实参顺序完全跟随 aclnn 接口的参数表：可选张量 `gamma_2`（`c10::optional`）、两个 double、最后两个是输出张量；返回 `std::tuple`。
- [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/csrc/npu_ai_infra_mhc_sandwich_norm_post_preonly.cpp:53-83](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/csrc/npu_ai_infra_mhc_sandwich_norm_post_preonly.cpp#L53-L83)：v2 的 `construct_output_tensors_v2`——只有 `return_h_in_f32=true` 时才分配第三个输出 `h_in_f32`（FP32）；第 59 行注释说明：默认构造的空 `at::Tensor` 传到 opapi 层转成 `aclTensor*` 后就是 `nullptr`，即「可选输出」向下传达 None 的方式。
- [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/csrc/npu_ai_infra_mhc_sandwich_norm_post_preonly.cpp:114-129](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/csrc/npu_ai_infra_mhc_sandwich_norm_post_preonly.cpp#L114-L129)：v1/v2 的 Meta 实现——函数体只有一行 `return construct_output_tensors(...)`，把 shape 推导完全交给共享函数，没有 EXEC。
- [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/csrc/npu_ai_infra_mhc_sandwich_norm_post_preonly.cpp:136-149](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/csrc/npu_ai_infra_mhc_sandwich_norm_post_preonly.cpp#L136-L149)：每块 IMPL 挂两条（v1 与 v2），PrivateUse1 挂 `_npu` 后缀的真算函数，Meta 挂 `_meta` 后缀的推形状函数——仓库的命名惯例。

**str 参数的实现侧处理**（rotary_mul）：

- [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/posembedding/ai_infra_rotary_position_embedding/csrc/npu_ai_infra_rotary_mul.cpp:35-39](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/posembedding/ai_infra_rotary_position_embedding/csrc/npu_ai_infra_rotary_mul.cpp#L35-L39)：`c10::string_view rotary_mode` 接住 schema 的 `str rotary_mode='half'`，并用 `TORCH_CHECK` 校验取值必须为 `"half"`——**枚举型字符串参数的合法性检查在 csrc 层做**。

#### 4.3.4 代码实践

**实践目标**：体验「同一算子、两个键、两条路径」。

**操作步骤**（需要装有 wheel 包的环境，待本地验证；无环境时完成步骤 3 的纯阅读版）：

1. 运行下面这段对照脚本（示例代码）：

   ```python
   import torch, torch_npu, omni_custom_ops

   x = torch.randn(2, 3, 4, 5, 6).npu()
   out = torch.ops.custom.npu_lower_triangular_inverse(x)      # 走 PrivateUse1
   print(out.shape)

   xm = torch.randn(2, 3, 4, 5, 6, device='meta')
   outm = torch.ops.custom.npu_lower_triangular_inverse(xm)    # 走 Meta
   print(outm.shape, outm.device)
   ```

2. 把输入换成 CPU 张量再调用一次，观察报错。
3. 纯阅读版：对照 [lower_triangular_inverse.cpp:20-33](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp#L20-L33) 两个函数，列出「NPU 实现比 Meta 实现多的那一行」，并解释 mhc 算子为什么把这段公共逻辑抽成 `construct_output_tensors`。

**需要观察的现象**：

- 第 1 步两条调用都成功，且 `out.shape == xm 推导出的 outm.shape == (2, 3, 4, 5, 6)`；`outm.device` 是 meta——**Meta 路径没有发生任何真实计算**，只返回了形状信息。
- 第 2 步 CPU 张量调用报错——因为仓库只注册了 PrivateUse1 与 Meta 两个键，没有 CPU 实现。

**预期结果**：理解「键 = 按设备选实现的开关」，以及 Meta 实现对图编译/形状预推导的意义。

#### 4.3.5 小练习与答案

**练习 1**：Meta 实现里为什么常用 `at::empty_symint` / `sym_size`，而 lower_triangular_inverse 的实现用了 `at::empty_like` 也可以？

**答案**：`empty_like` 隐式复制输入的全部形状信息，对「输出形状与输入全同」的算子最省事；一旦输出形状需要重组（如 scatter 的取前三维、mhc 的按维度分支拼新形状），就要用 `sym_size` 取符号尺寸再 `empty_symint` 构造。符号 API 在图编译期尺寸未知时仍能完成推导，是更通用的写法。

**练习 2**：scatter 非原地版的 Meta 实现返回 `{size(0), size(1), size(2)}` 三维输出，而 NPU 版是把 `input.clone()` 整个返回（五维）。两者矛盾吗？

**答案**：不矛盾——4.3.3 已看到非原地版 NPU 实现返回的是 clone 的完整输入（五维），Meta 版返回的形状推导是按本算子语义（更新前三维索引的行）写的输出形状。这提示 Meta 推导是**人工维护**的，若与 NPU 实现不一致，图模式编译期推出的形状会和真实运行不符。这也再次印证 u1-l3 的提醒：文档与推导代码可能滞后，以 NPU 实现为准。（读者可带着这个疑问在 ST 测试中验证真实输出形状——待本地验证。）

**练习 3**：为什么每个算子都要写 Meta 实现？只写 PrivateUse1 行不行？

**答案**：eager 模式下只写 PrivateUse1 也能跑。但 `device='meta'` 的张量、`torch.compile` / torchair 图模式的编译期 shape 推导都会请求 Meta 键，没有实现就推导失败或退化为真实执行。仓库为保持对图模式的兼容（配合第 3 单元 u3-l3 的 converter），给每个算子都补了 Meta 实现。

## 5. 综合实践

**任务：为假想算子 `my_add`（逐元素相加，`z = x + y`）补全 PyTorch 侧注册**——把本讲三个模块（先定义 → 签名语法 → 双键实现）串成一次完整操作。以下改动请在**你自己 fork 的副本**里做，不要改动上游仓库；算子后端（aclnn 接口与 kernel）不存在，本实践只验证「注册链路」而非数值正确性。

### 步骤 1：补写 m.def 签名

在 [ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L164-L165) 的 `TORCH_LIBRARY_FRAGMENT` 块末尾（第 164 行 matmul 定义之后）加一行（示例代码）：

```cpp
m.def("npu_my_add(Tensor x, Tensor y) -> Tensor");
```

### 步骤 2：新建 csrc 实现文件

新建目录与文件 `omni_custom_ops/ops_transformer/index/my_add/csrc/my_add.cpp`（示例代码）：

```cpp
#include <torch/library.h>
#include "../../../../csrc_base/ops_common.h"   // 提供 EXEC_NPU_CMD_V1（与仓库各 csrc 同款相对路径）

namespace custom {

// PrivateUse1：NPU 真算
at::Tensor npu_my_add(const at::Tensor &x, const at::Tensor &y)
{
    at::Tensor out = at::empty_like(x);
    EXEC_NPU_CMD_V1(aclnnMyAdd, x, y, out);   // 假想 op_api 层存在 aclnnMyAdd(x, y, out)
    return out;
}

// Meta：只推形状
at::Tensor npu_my_add_meta(const at::Tensor &x, const at::Tensor &y)
{
    return at::empty_like(x);
}

} // namespace custom

TORCH_LIBRARY_IMPL(custom, PrivateUse1, m) {
    m.impl("npu_my_add", &custom::npu_my_add);
}

TORCH_LIBRARY_IMPL(custom, Meta, m) {
    m.impl("npu_my_add", &custom::npu_my_add_meta);
}
```

### 步骤 3：确认文件会被自动收集

检查 [ascendc/torch_ops_extension/setup.py:49-50](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/setup.py#L49-L50)：两条 glob 规则是 `omni_custom_ops/csrc_base/*.cpp` 与 `omni_custom_ops/*/*/*/csrc/*.cpp`。你放置的路径 `omni_custom_ops/ops_transformer/index/my_add/csrc/my_add.cpp` 恰好匹配第二条（`ops_transformer / index / my_add / csrc` 四级），**无需改 setup.py**。相对 include 路径 `../../../../csrc_base/ops_common.h` 从 csrc 目录向上四级正好回到 `omni_custom_ops/`，与 scatter 算子文件（[npu_ai_infra_scatter_block_update.cpp:13](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/index/ai_infra_scatter_block_update/csrc/npu_ai_infra_scatter_block_update.cpp#L13)）一致。

### 步骤 4：重新编译并验证

1. 参考 u1-l4 的 `build_and_install.sh` 重新打包安装 wheel。
2. 验证注册（示例代码）：

   ```python
   import torch, torch_npu, omni_custom_ops
   print(torch.ops.custom.npu_my_add.default._schema)   # 应打印 npu_my_add(Tensor x, Tensor y) -> Tensor
   xm = torch.randn(2, 3, device='meta')
   ym = torch.randn(2, 3, device='meta')
   print(torch.ops.custom.npu_my_add(xm, ym).shape)     # 走 Meta：应输出 torch.Size([2, 3])
   ```

3. NPU 真值调用会因 `aclnnMyAdd` 符号不存在而失败——这是预期的：注册链路（本讲主题）已通，后端算子（u2 系列 + u3-l2 的 EXEC 机制）才是下一步。

**预期结果**：schema 打印正确、meta 路径返回正确形状。若 `torch.ops.custom` 里找不到 `npu_my_add`，按顺序检查：① m.def 是否真的写进了 `TORCH_LIBRARY_FRAGMENT` 块；② 新文件路径是否匹配 setup.py 的 glob；③ 是否重新编译并重开了 Python 进程。无硬件/编译环境时，步骤 1-3 的静态检查结论依然成立，步骤 4 标注「待本地验证」。

## 6. 本讲小结

- csrc 层的注册是**两步走**：先在 `csrc_base/ops_def_registration.cpp` 的 `TORCH_LIBRARY_FRAGMENT(custom, m)` 里 `m.def` 签名（文件内注释原话「step1, 为新增自定义算子添加定义」），再在算子自己的 csrc 文件里 `TORCH_LIBRARY_IMPL` 挂实现；def 与 impl 分居两文件、缺一不可。
- `m.def` 的 schema DSL 规定了参数类型（`Tensor`/`Tensor?`/`Tensor(a!)`/`int`/`float`/`str`/`bool`/`SymInt[]?`）、默认值、`*` 后的关键字参数区、多返回值 `-> (Tensor, Tensor)` 与原地版本 `-> ()`；每个 schema 类型在实现侧有固定的 C++ 形参类型（如 `str` → `c10::string_view`、`Tensor?` → `c10::optional<at::Tensor>`）。
- 每个算子挂两个调度键：`PrivateUse1`（torch_npu 的 NPU 设备键，真算：构输出 → `EXEC_NPU_CMD_V1(aclnnXxx, ...)` → 返回）与 `Meta`（只推形状不计算，服务于 meta 设备与图模式编译期推导）；两者常共享 shape 构造函数。
- 原地算子（`Tensor(a!)`、名字带 `_`、返回 `void`）与非原地算子可共用同一个 aclnn 接口，非原地语义靠 csrc 层 `clone` 实现。
- 新增算子的 csrc 文件必须放在 `omni_custom_ops/<组>/<族>/<算子>/csrc/` 下才会被 setup.py 的 glob 自动收集，include 用 `../../../../csrc_base/ops_common.h` 相对路径。
- 一切注册都发生在 `import omni_custom_ops` 加载 `.so` 的瞬间；`PYBIND11_MODULE` 模块体为空，因为暴露走的是 PyTorch 分发器而非 pybind11。

## 7. 下一步学习建议

本讲只把 `EXEC_NPU_CMD_V1(aclnnMyAdd, x, y, out)` 当成黑盒。下一讲 **u3-l2《ops_common 通用适配层：EXEC_NPU_CMD_V1 与类型转换》**将打开这个宏：`at::Tensor` 如何转成 `aclTensor`、aclnn 符号如何经 dlopen/dlsym 从 vendors 目录找到、两段式接口（GetWorkspaceSize + 执行）如何被宏串起来。之后再进入 **u3-l3（converter 与 torchair 图模式）**，看本讲的 Meta 实现与 converter 如何配合支撑图执行，最后在 **u3-l4** 复盘从 `torch.ops.custom.npu_xxx` 到 kernel 的完整端到端调用链。建议同步精读的源码：`omni_custom_ops/csrc_base/ops_common.h` 与 `ops_common.cpp`。
