# torch_ops_extension 总览：从 aclnn 到 torch.ops.custom

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `torch_ops_extension` 在整个项目中的位置：它是把第 1～5 单元讲解的 Ascend C 算子（aclnn 接口）包装成 PyTorch 可调用算子的「适配层」，是训练脚本的最终消费入口。
2. 解释 `setup.py` 如何用两条 `glob` 规则自动收集 15 个 `.cpp` 源文件、如何用 torch_npu 提供的 `NpuExtension` 构建扩展模块 `custom_ops_lib`。
3. 独立完成 wheel 包的编译、安装与 `import` 验证（或在无 NPU 环境下完成等效的静态分析验证）。
4. 说明 `csrc_base` 公共层的职责：`ops_def_registration.cpp` 负责 torch 侧算子 schema 注册，`ops_common.h/.cpp` 负责 at::Tensor ↔ acl 类型的转换与 aclnn 符号的运行期动态查找（`EXEC_NPU_CMD_V1` 宏）。

## 2. 前置知识

本讲默认你已读过 u1-l4（`build.sh` 编译算子 run 包）。需要回顾或新引入的概念：

- **aclnn 接口**：昇腾算子对外的 C 接口，形如 `aclnnAiInfraAggregateHidden`，分「GetWorkspaceSize + 执行」两段式（详见 u2-l5）。它是 C 接口，接收的是 `aclTensor*` 等 C 结构体，不能直接接收 `torch::Tensor`。
- **PyTorch 扩展（extension）**：用 C++ 写代码、通过 pybind11 编译成 `.so`，让 Python 里能调用 C++ 函数的机制。`torch.utils.cpp_extension` 是官方构建工具。
- **torch_npu**：PyTorch 的昇腾后端适配包。它提供的 `NpuExtension` 是 `CPPExtension` 的 NPU 增强版——自动追加 NPU 编译宏与链接项。本仓库的 csrc 代码 include 了大量 `torch_npu/csrc/...` 内部头文件，因此构建时必须 import torch_npu。
- **wheel 包**：Python 的二进制分发格式（`.whl`），`pip install` 即装。装完后 `import omni_training_custom_ops` 就能拿到编译好的 `.so` 和 Python 代码。
- **`torch.ops` 命名空间机制**：PyTorch 允许通过 `TORCH_LIBRARY` 系列宏把自定义算子 schema 注册到一个命名空间（本仓库用 `custom`），之后用 `torch.ops.custom.算子名(...)` 调用，并可自动接入 autograd。这比 pybind11 直接导出函数更「torch 原生」。
- **dlsym/dlopen**：Linux 动态库接口，运行期按名字从 `.so` 中查找函数地址。本讲的关键设计——aclnn 符号不在编译期链接，而在运行期从已安装的算子包 `libcust_opapi.so` 中按名查找——就靠它实现。

**一句话定位本讲**：u1-l4 把算子源码编成 CANN 算子包（run 包），本讲把「调用这些算子」这件事包装成一个 pip 包；两层产物缺一不可。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `ascendc/torch_ops_extension/setup.py` | wheel 构建入口：glob 收集源码、声明 NpuExtension 扩展模块 |
| `ascendc/torch_ops_extension/build_and_install.sh` | 三步脚本：清理 → 编译 wheel → pip 安装 |
| `.../omni_training_custom_ops/csrc_base/ops_def_registration.cpp` | torch 侧算子 schema 注册（TORCH_LIBRARY_FRAGMENT）+ pybind11 绑定 |
| `.../omni_training_custom_ops/csrc_base/function.h` | 声明 `custom` 命名空间各算子的 C++ autograd 接口 |
| `.../omni_training_custom_ops/csrc_base/ops_common.h` | 公共适配层头文件：acl 类型转换、aclnn 符号动态查找、`EXEC_NPU_CMD_V1` 宏 |
| `.../omni_training_custom_ops/csrc_base/ops_common.cpp` | 公共适配层实现：全局查找路径初始化、feature 库遍历 |
| `.../omni_training_custom_ops/__init__.py` | 包入口：加载 `.so` 并把 `torch.ops.custom.*` 挂载到 `torch_npu` |
| `.../ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp` | 算子适配层样例（u6-l2 精读，本讲只看它如何引用公共层） |
| `ascendc/README.md` | 官方编译安装说明（torch_ops_extension 章节） |

目录组织约定（与 `ops-transformer` 的 attention/mhc/mome 家族划分一致）：

```text
torch_ops_extension/
├── setup.py
├── build_and_install.sh
└── omni_training_custom_ops/                 # Python 包根
    ├── __init__.py                           # 包入口（挂载逻辑）
    ├── csrc_base/                            # 公共适配层（本讲重点）
    │   ├── function.h
    │   ├── ops_common.h / ops_common.cpp
    │   └── ops_def_registration.cpp
    └── ops_transformer/                      # 按 attention / mhc / mome 分家族
        ├── attention/<算子名>/csrc/*.cpp      # 算子适配层（u6-l2 精读）
        ├── mhc/<算子名>/{csrc,converter,test}/
        └── mome/aggregate_hidden/csrc/*.cpp
```

注意与 ascendc 侧的区别：mhc 家族的目录里还带 `converter/`（Python 参数转换）与 `test/`（单算子验证），即「csrc + converter + test 三件套」，u6-l3 会专门讲。

## 4. 核心概念与源码讲解

### 4.1 torch_ops_extension 的位置与包结构

#### 4.1.1 概念说明

前五个单元里我们一直在读「算子本体」：`_def.cpp` 注册原型、tiling 切分、kernel 计算。这些编出来的是 CANN 算子包，对外暴露的是 C 语言接口 `aclnnXxx`。但盘古 2.0 的训练脚本是 PyTorch 写的，张量是 `torch.Tensor`，期望的调用方式是 `y = torch_npu.npu_aggregate_hidden(x, w)` 并且能自动反向传播。

`torch_ops_extension` 就是补上这「最后一公里」的翻译层：

```text
torch.Tensor (Python)
   │  torch.ops.custom.npu_aggregate_hidden / torch_npu.npu_aggregate_hidden
   ▼
custom 命名空间 schema 分发 (ops_def_registration.cpp 注册)
   │  C++ 层：at::Tensor 参数 → EXEC_NPU_CMD_V1 宏
   ▼
ops_common.h：at::Tensor → aclTensor* 转换 + dlsym 查找 aclnn 符号
   │  aclnnAiInfraAggregateHidden(GetWorkspaceSize + 执行)
   ▼
已安装的 CANN 算子包 libcust_opapi.so (u1-l4 的 run 包产物)
   │  tiling → kernel launch
   ▼
NPU 硬件
```

#### 4.1.2 核心流程

从「pip 装好 wheel」到「能调用算子」的完整时序：

1. 训练脚本 `import omni_training_custom_ops`。
2. `__init__.py` 先 `import torch`、`import torch_npu`（保证 torch 的注册表已就绪），再 `from . import custom_ops_lib` 加载编译好的 `.so`。
3. `.so` 加载时执行 `ops_common.cpp` 里的全局变量初始化，读取 `ASCEND_CUSTOM_OPP_PATH` / `ASCEND_OPP_PATH` 环境变量，生成 aclnn 符号查找路径列表（这就是为什么必须先 source 算子包的 `set_env.bash`）。
4. `.so` 里的 `TORCH_LIBRARY_FRAGMENT(custom, m)` 静态注册代码执行，`torch.ops.custom` 命名空间下出现全部算子 schema。
5. `__init__.py` 把 `torch.ops.custom` 下每个算子 `setattr` 到 `torch_npu` 模块上，于是 `torch_npu.npu_xxx(...)` 也可用。
6. 用户调用算子 → 分发到 C++ 实现 → `EXEC_NPU_CMD_V1` 按算子名 dlsym 查找 aclnn 两段式接口 → 下发到 NPU。

#### 4.1.3 源码精读

包入口的挂载逻辑：

[omni_training_custom_ops/__init__.py:9-21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/__init__.py#L9-L21)

文件开头的 docstring 直接说明了两种预期用法：`torch.ops.custom.npu_selected_flash_attention()` 与 `torch_npu.npu_selected_flash_attention()`。第 18-19 行先导入 torch 与 torch_npu，注释写明是为了「避免后续挂载操作失败」——顺序错了 torch 的算子注册表还没建好。第 21 行 `from . import custom_ops_lib` 触发 `.so` 加载，这是整条链的开关。

[omni_training_custom_ops/__init__.py:25-41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/__init__.py#L25-L41)

`getattr(torch.ops, 'custom', None)` 拿到 `.so` 注册出的命名空间对象，遍历其中所有非下划线开头的名字，逐一 `setattr(torch_npu, op_name, custom_op_func)`——把算子「搬」到 torch_npu 模块上。若 `torch.ops.custom` 不存在则只发 warning 并降级为「必须用 `torch.ops.custom.xxx` 调用」。

#### 4.1.4 代码实践

1. **实践目标**：在不安装的情况下，仅凭目录结构推断 wheel 里会装什么。
2. **操作步骤**（无 NPU 环境可做）：
   ```bash
   cd training/ascendc/torch_ops_extension
   # Python 包部分：哪些目录有 __init__.py，find_packages() 就会收集哪些
   find omni_training_custom_ops -name "__init__.py" | sort
   ```
3. **需要观察的现象**：每个 attention/mhc/mome 家族下的算子目录几乎都有自己的 `__init__.py`，唯独 `sparse_lightning_indexer_grad_kl_loss_enhance` 没有。
4. **预期结果**：`find_packages()` 收集的是「带 `__init__.py` 的目录」，所以该算子目录不会成为 Python 子包；但下一节会看到它的 `.cpp` 依然会被编进 `.so`（glob 只看路径模式，不看 `__init__.py`）——它的算子仍可通过 `torch.ops.custom` 调用，只是没有 Python converter 层。这是目录约定里一个值得注意的真实例外。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `__init__.py` 里必须先 `import torch_npu` 再 `from . import custom_ops_lib`，反过来行不行？

**答案**：`custom_ops_lib.so` 的编译与运行都依赖 torch_npu：构建期 `setup.py` 需要 torch_npu 的头文件路径，运行期 `.so` 中 `TORCH_LIBRARY_FRAGMENT` 注册 schema 需要 torch 的库已加载、`ops_common.cpp` 的全局初始化依赖 torch_npu 的环境。若先加载 `.so`，其依赖的动态库与注册表可能未就绪，导致 import 报错或注册丢失。此外 `__init__.py` 后半段要把算子挂到 `torch_npu` 模块对象上，也要求 `torch_npu` 已在当前命名空间。

**练习 2**：`torch.ops.custom.npu_xxx()` 和 `torch_npu.npu_xxx()` 两种调用方式最终指向同一个对象吗？

**答案**：是。`__init__.py` 第 34-35 行 `setattr(torch_npu, op_name, custom_op_func)` 中的 `custom_op_func` 就是 `getattr(custom_ops_module, op_name)` 取出的同一个 `torch.ops.custom.npu_xxx` 对象，只是多了一个模块属性别名，二者调用等价。

### 4.2 setup.py：glob 自动收集与 NpuExtension 构建

#### 4.2.1 概念说明

`setup.py` 是 Python 打包的标准入口，本仓库只有 47 行，核心就做两件事：

1. **收集源码**：用两条 `glob` 模式自动发现所有要编译的 `.cpp`，新增算子目录时**不需要改 setup.py**——这是「约定优于配置」的设计。
2. **声明扩展**：把源码列表交给 `NpuExtension`，产出一个名为 `omni_training_custom_ops.custom_ops_lib` 的扩展模块。

`NpuExtension` 来自 `torch_npu.utils.cpp_extension`，是 torch_npu 对 PyTorch 官方 `CPPExtension` 的封装，会自动附加昇腾相关的编译定义、头文件搜索路径与链接库，使得 csrc 代码能直接 include `torch_npu/csrc/...` 的内部头。

#### 4.2.2 核心流程

```text
setup.py 执行
  ├─ import torch → import torch_npu（构建期就要求两包可用）
  ├─ glob 规则①: omni_training_custom_ops/csrc_base/*.cpp      → 2 个文件
  ├─ glob 规则②: omni_training_custom_ops/*/*/*/csrc/*.cpp     → 13 个文件
  ├─ NpuExtension(name="...custom_ops_lib", sources=15 个文件,
  │              extra_compile_args=[-I torch_npu/acl/inc])
  └─ setup(name="omni_training_custom_ops", version='1.0',
           ext_modules=[ext], packages=find_packages(),
           cmdclass={"build_ext": BuildExtension.with_options(use_ninja=USE_NINJA)})
```

规则②的路径模式 `*/*/*/csrc` 固定了三层目录深度：`ops_transformer / <家族> / <算子名> / csrc`。这意味着如果有人把算子适配放在别的深度（比如直接 `ops_transformer/<算子名>/csrc`），会被静默漏掉——编译不出错，但算子缺失，是比较隐蔽的坑。

#### 4.2.3 源码精读

[setup.py:15-20](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/setup.py#L15-L20)

导入 `torch_npu` 并从其 `utils.cpp_extension` 引入 `NpuExtension`——**构建这个包的机器必须已装好 torch 与 torch_npu**，因为第 18 行还要取 `torch_npu` 的安装路径。第 19 行 `USE_NINJA` 读环境变量决定是否用 ninja 加速编译，默认关闭（走传统 make）。

[setup.py:23-24](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/setup.py#L23-L24)

两条源码收集规则。规则①收公共层 `csrc_base` 的全部顶层 `.cpp`（`ops_def_registration.cpp` 与 `ops_common.cpp`）；规则②用 `*/*/*/csrc` 匹配每个算子目录下的 `csrc/*.cpp`。经实际 glob 验证，共收集 15 个文件：`csrc_base` 2 个 + attention 家族 6 个 + mhc 家族 6 个 + mome 家族 1 个（完整清单见 4.2.4 实践）。

[setup.py:27-33](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/setup.py#L27-L33)

`NpuExtension` 的名字是**带点号的模块全名** `omni_training_custom_ops.custom_ops_lib`——setuptools 会据此把编译产物 `custom_ops_lib.so` 放进 `omni_training_custom_ops` 包目录，`__init__.py` 里 `from . import custom_ops_lib` 才能找到它。`extra_compile_args` 只手动加了一个 include：torch_npu 包内 `include/third_party/acl/inc`（ACL 头文件，`ops_common.h` 第 20-23 行 `#include <acl/acl_base.h>` 等依赖它）；csrc_base 与各算子 csrc 之间则靠**相对路径 include** 打通（见 4.4.3 的 `#include "../../../../csrc_base/ops_common.h"`），所以不需要额外的 `-I`。

[setup.py:36-47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/setup.py#L36-L47)

`setup()` 声明包名 `omni_training_custom_ops`、版本 1.0；`package_data` 把包内 `.py` 与 `.so` 都计入分发物；`packages=find_packages()` 自动发现所有带 `__init__.py` 的子包（即各算子的 converter 层）；`cmdclass` 用 `BuildExtension.with_options(use_ninja=USE_NINJA)` 替换默认构建命令——这是 PyTorch 官方推荐做法，会自动加上 `-DTORCH_API_INCLUDE_EXTENSION_H` 等必需编译参数并处理 C++ 标准选择。

#### 4.2.4 代码实践

**实践 A：验证 glob 收集清单（无 NPU 环境可完成）**

1. **实践目标**：列出 setup.py 实际会编译的 15 个 `.cpp`，验证你对两条 glob 规则的理解。
2. **操作步骤**：
   ```bash
   cd training/ascendc/torch_ops_extension
   python3 - <<'EOF'
   import glob, os
   BASE = os.getcwd()
   s  = glob.glob(os.path.join(BASE, "omni_training_custom_ops/csrc_base", "*.cpp"))
   s += glob.glob(os.path.join(BASE, "omni_training_custom_ops/*/*/*/csrc", "*.cpp"))
   print("total:", len(s))
   for f in sorted(os.path.relpath(x, BASE) for x in s):
       print(f)
   EOF
   ```
   这段脚本与 setup.py 第 23-24 行的逻辑逐字一致，只是把结果打印出来。
3. **需要观察的现象**：输出 total 为 15，文件按 `csrc_base` → attention → mhc → mome 排列。
4. **预期结果**（笔者已在本仓库 HEAD 上验证）：

   | 类别 | 文件 |
   |---|---|
   | csrc_base（2） | `csrc_base/ops_def_registration.cpp`、`csrc_base/ops_common.cpp` |
   | attention（6） | `ai_infra_attention_pioneer/csrc/npu_ai_infra_attention_pioneer.cpp`、`ai_infra_attention_pioneer_metadata/csrc/npu_ai_infra_attention_pioneer_metadata.cpp`、`flash_attention_score_enhance/csrc/npu_flash_attention_score_enhance.cpp`、`lightning_indexer_enhance/csrc/npu_lightning_enhance_indexer.cpp`、`sparse_flash_attention_enhance/csrc/sparse_flash_attention_enhance.cpp`、`sparse_lightning_indexer_grad_kl_loss_enhance/csrc/npu_sparse_lightning_indexer_grad_kl_loss_enhance.cpp` |
   | mhc（6） | `ai_infra_manifold_constrained_hyper_connection_post/csrc/npu_ai_infra_manifold_constrained_hyper_connection_post.cpp`、`..._post_grad/csrc/npu_ai_infra_manifold_constrained_hyper_connection_post_grad.cpp`、`ai_infra_mhc_post_grad/csrc/npu_ai_infra_mhc_post_grad.cpp`、`manifold_constrained_hyper_connection_pre/csrc/npu_manifold_constrained_hyper_connection_pre.cpp`、`sinkhorn/csrc/npu_sinkhorn.cpp`、`sinkhorn_grad/csrc/npu_sinkhorn_grad.cpp` |
   | mome（1） | `aggregate_hidden/csrc/npu_aggregate_hidden.cpp` |

   注意：`.h` 头文件（`function.h`、`ops_common.h`、各算子头文件）都不在列表里——只编译 `.cpp`，头文件靠 include 引用；`ops_common.h` 之所以能被 `ops_common.cpp` 之外的整体使用，是因为每个算子 csrc 都用相对路径 include 它。

**实践 B：编译期 dry-run（需要 torch + torch_npu，不需要 NPU 硬件）**

在装了 torch_npu 的容器里执行 `python3 setup.py build --dry-run` 或直接 `python3 setup.py build`，观察 setuptools 输出的编译命令里 torch_npu 注入的头文件路径与 `-I` 参数。（完整编译安装见 4.5 的实践。）

#### 4.2.5 小练习与答案

**练习 1**：新增算子适配 `ops_transformer/mome/new_op/csrc/npu_new_op.cpp` 后，setup.py 需要修改吗？

**答案**：不需要。规则② `omni_training_custom_ops/*/*/*/csrc/*.cpp` 会自动发现它。需要改的是 `csrc_base/ops_def_registration.cpp`——在 `TORCH_LIBRARY_FRAGMENT(custom, m)` 里加 `m.def` schema（文件第 15-16 行的注释明确说了「每次新增自定义aten ir都需先增加定义」），并在 `function.h` 声明对应 C++ 函数。

**练习 2**：如果把某个算子的 csrc 目录从 `ops_transformer/mhc/xxx/csrc/` 移到 `ops_transformer/mhc/xxx/ccsrc/`，会发生什么？

**答案**：glob 规则②匹配的是目录名 `csrc`，改名后该 `.cpp` 不再被收集，编译不报错但 `.so` 里缺少该算子实现；而注册层 `ops_def_registration.cpp` 仍声明了 schema（如果加了的话），链接期若 schema 绑定引用了缺失符号会报 undefined reference，若只在 torch 层注册则会运行期才发现算子无实现。这类「静默遗漏」正是 glob 自动收集的代价。

**练习 3**：`USE_NINJA=1` 环境变量影响什么？

**答案**：`setup.py:19` 读取它并在第 46 行传给 `BuildExtension.with_options(use_ninja=...)`，决定 build_ext 阶段用 ninja（并行、增量更快）还是默认的 make 流程。默认值 `'1' == os.getenv('USE_NINJA')` 为 False，即默认不用 ninja。

### 4.3 csrc_base 公共层之一：ops_def_registration.cpp 的 schema 注册

#### 4.3.1 概念说明

算子要在 torch 生态里被「正规军」式调用（走 dispatcher、支持 autograd、支持 meta 推 shape），必须先注册 schema——一条声明「算子名、参数类型、返回类型」的字符串，格式与 torch 内置 aten 算子一致。`ops_def_registration.cpp` 就是全仓库 18 个自定义算子的 schema 总账本。

它同时做两件事：

1. **`TORCH_LIBRARY_FRAGMENT(custom, m)`**：把 schema 注册进 `custom` 命名空间 → 产生 `torch.ops.custom.npu_xxx`。用 FRAGMENT 而不是完整 `TORCH_LIBRARY`，是为了允许多个文件（未来每个算子 csrc 也可能各自注册 impl）共同向同一命名空间追加定义。
2. **`PYBIND11_MODULE`**：把 8 个 autograd 封装函数导出为 Python 可直接 `import` 的函数。这是「备胎通道」——即使不走 dispatcher 也能调用。

#### 4.3.2 核心流程

```text
.so 加载
  ├─ TORCH_LIBRARY_FRAGMENT(custom, m) 执行
  │    m.def("npu_aggregate_hidden(Tensor input, Tensor weight, *, Tensor? mask=None) -> Tensor")
  │    ... 共 18 条 schema
  │    → torch.ops.custom 命名空间出现 18 个算子（暂无实现，实现由各算子 csrc 的
  │      TORCH_LIBRARY_IMPL 注册，见 u6-l2）
  └─ PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) 执行
       m.def("npu_aggregate_hidden", &custom::npu_aggregate_hidden_autograd, ...)
       ... 共 8 个函数绑定
       → omni_training_custom_ops.custom_ops_lib.npu_xxx 可直接调用
```

#### 4.3.3 源码精读

[ops_def_registration.cpp:11-18](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp#L11-L18)

第 15-17 行的中文注释写明了设计意图：「在custom命名空间里注册后续的XXX算子，每次新增自定义aten ir都需先增加定义」。`TORCH_LIBRARY_FRAGMENT(custom, m)` 中的 `m` 是 torch 传入的注册句柄，`m.def(...)` 一条条挂 schema。第 13 行 `#include "function.h"` 引入同目录的函数声明——注册的 schema 与 C++ 声明必须类型对齐。

[ops_def_registration.cpp:44-47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp#L44-L47)

拿本手册的老朋友 aggregate_hidden 举例：schema `npu_aggregate_hidden(Tensor input, Tensor weight, *, Tensor? mask=None) -> Tensor`——`*` 之后是仅关键字参数，`Tensor?` 表示可选张量，与 u2-l2 读过的 `_def.cpp` 中 mask 为 OPTIONAL 输入完全对应。反向 `npu_aggregate_hidden_grad` 返回 `(Tensor, Tensor)` 即 grad_input 与 grad_weight 两个输出。这体现了同一算子在 CANN 侧（`_def.cpp` 的 Input/Output）与 torch 侧（schema 的参数/返回值）两套平行声明，语义一致但语法不同。

[ops_def_registration.cpp:92-110](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_def_registration.cpp#L92-L110)

`PYBIND11_MODULE(TORCH_EXTENSION_NAME, m)` 是扩展模块的 Python 入口（`TORCH_EXTENSION_NAME` 宏会被 BuildExtension 定义为 `omni_training_custom_ops.custom_ops_lib`）。这里绑定的注释也写得很清楚：「绑定的是接口不是算子」——即绑定的是 `custom::` 命名空间下带 autograd 拼接的封装函数（如第 96 行 `&custom::npu_aggregate_hidden_autograd`），而非裸 aclnn 调用。值得注意的细节：`npu_ai_infra_attention_pioneer` 这个名字在第 103-106 行与 107-109 行被绑定了两次（一次普通版、一次 `_autograd` 版），pybind11 会将同名绑定作为重载处理——哪个版本实际生效建议本地验证（「待本地验证」）。

[function.h:17-51](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/function.h#L17-L51)

`function.h` 声明 `namespace custom` 下的 C++ 接口。第 47-51 行 `npu_aggregate_hidden_autograd` 的签名与 schema 一一对应（`int64_t` 对应 `int`、`c10::optional<at::Tensor>` 对应 `Tensor?`），各算子 csrc 的 `.cpp` 提供函数体，链接成一个 `.so` 后符号在此汇合。这就是 4.2 节「15 个 cpp 编一个扩展」在代码组织上的意义。

#### 4.3.4 代码实践

1. **实践目标**：统计 schema 总账本，建立「torch 侧算子全景」。
2. **操作步骤**：
   ```bash
   cd training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base
   grep -c "m.def(" ops_def_registration.cpp
   grep -o 'm\.def("npu_[a-z_0-9]*"' ops_def_registration.cpp | sort
   grep -o 'm\.def("npu_[a-z_0-9]*"' ops_def_registration.cpp | wc -l
   ```
3. **需要观察的现象**：第一个命令给出 `TORCH_LIBRARY_FRAGMENT` 内 `m.def` 的条数；第三个命令给出 `PYBIND11_MODULE` 内的绑定条数。
4. **预期结果**：注册块内共 18 条 schema（覆盖前向/反向/元数据算子，含 FA、sparse FA、lightning indexer、sinkhorn、MHC pre/post、pioneer、aggregate_hidden 等）；pybind 块内 8 条绑定（含 `npu_ai_infra_attention_pioneer` 的两次同名绑定）。对比两张清单可以发现：**大部分算子只注册 schema 不做 pybind 绑定**，它们的调用入口是 `torch.ops.custom.npu_xxx`。

#### 4.3.5 小练习与答案

**练习 1**：schema 里 `Tensor? mask=None` 与 `_def.cpp` 里的 `OPTIONAL()` 输入是什么关系？

**答案**：同一语义的两层表达。CANN 侧 `_def.cpp` 用 `Input("mask").Optional()` 声明该输入可选（u2-l2）；torch 侧 schema 用 `Tensor?` + 默认值 `None` 表达可选。适配层的职责之一就是保证两侧一致——mask 为 None 时直接把空 optional 传给 aclnn（见 u6-l2 的 `EXEC_NPU_CMD_V1(aclnnAiInfraAggregateHidden, input, weight, mask, output)`，注释「mask为空直接传」）。

**练习 2**：为什么用 `TORCH_LIBRARY_FRAGMENT` 而不是 `TORCH_LIBRARY`？

**答案**：`TORCH_LIBRARY` 声明「整个命名空间由本处独占定义」，重复定义会冲突；`FRAGMENT` 允许多个编译单元向同一命名空间各自追加 schema/实现。本仓库把全部 schema 集中在一个文件里，但各算子的 **impl**（TORCH_LIBRARY_IMPL，见 u6-l2）分散在各算子 csrc 中，正是 FRAGMENT 语义支持的「注册与实现分离」布局。

### 4.4 csrc_base 公共层之二：ops_common 的类型转换与 EXEC_NPU_CMD_V1

#### 4.4.1 概念说明

如果说 `ops_def_registration.cpp` 是「门面」，`ops_common.h`（约 1100 行）就是「发动机」。它解决适配层最核心的两个问题：

1. **类型转换**：torch 的 `at::Tensor`/`Scalar`/`IntArrayRef` 与 ACL 的 `aclTensor*`/`aclScalar*`/`aclIntArray*` 是两套完全不同的类型体系，需要逐个转换（`ConvertType` 系列重载）。
2. **符号解析**：aclnn 接口不在编译期链接——因为算子包是**运行环境里安装的**（u1-l4 的 run 包，版本、路径都可能不同）。所以用 dlopen/dlsym 在**第一次调用时**按函数名查找 `aclnnXxx` 与 `aclnnXxxGetWorkspaceSize` 的地址。

这一切封装成一个宏 `EXEC_NPU_CMD_V1(aclnn_api, ...)`：各算子 csrc 只需一行就能发起一次完整的两段式 aclnn 调用。文件第 11-12 行的 include guard 名 `TORCHNPU_TORCH_NPU_CSRC_ATEN_OPS_OP_API_PTA_COMMON_H_` 暴露了它的出身——它源自 torch_npu 源码中 PTA（PyTorch Adapter）的 op_api 公共头，被拷贝进本仓库做了少量适配。

#### 4.4.2 核心流程

`EXEC_NPU_CMD_V1(aclnnAiInfraAggregateHidden, input, weight, mask, output)` 展开后的执行流程：

```text
1. static 查找符号（仅进程内首次执行）：
   GetOpApiFuncAddr("aclnnAiInfraAggregateHiddenGetWorkspaceSize")
   GetOpApiFuncAddr("aclnnAiInfraAggregateHidden")
   查找顺序（libcust_opapi.so 优先）：
     a. $ASCEND_CUSTOM_OPP_PATH 各路径 /op_api/lib/libcust_opapi.so   ← 自定义算子包
     b. $ASCEND_OPP_PATH/vendors/<load_priority 顺序>/op_api/lib/libcust_opapi.so
     c. libopapi_math/nn/cv/transformer/legacy.so → libopapi.so
     d. libaclnn_ops_infer/train/math/sparse/fft/rand.so（feature 库）
2. ConvertTypes(...)：把每个 at::Tensor 转成 aclTensor*（含 dtype/格式映射）
3. 调用第一段 GetWorkspaceSize(...) → 得到 workspace_size 与 executor
4. workspace_size != 0 时 at::empty 分配 NPU 上的 workspace 张量
5. 构造 acl_call lambda：调用第二段 opApiFunc(workspace, size, executor, stream)
6. at_npu::native::OpCommand::RunOpApiV2 异步下发到当前 NPU stream
7. ReleaseConvertTypes(...)：销毁所有 acl* 中间对象，释放大页内存/tls 缓存
```

其中第 1 步的 static 修饰意味着**符号地址在进程生命周期内只查一次**，之后直接复用——这就是「动态解析、一次成本」的设计。

#### 4.4.3 源码精读

先看算子 csrc 如何使用公共层：

[npu_aggregate_hidden.cpp:20](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp#L20)

`#include "../../../../csrc_base/ops_common.h"`——四个 `..` 从 `ops_transformer/mome/aggregate_hidden/csrc` 退回包根再进 csrc_base。相对路径 include 解释了 setup.py 为什么不需要为 csrc_base 加 `-I`：约定即路径。

[npu_aggregate_hidden.cpp:36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp#L36)

算子侧的全部调用就这一行：`EXEC_NPU_CMD_V1(aclnnAiInfraAggregateHidden, input, weight, mask, output)`。宏的 `#aclnn_api` 字符串化能力让「按名查找」成为可能——传进来的不是函数指针而是名字。

再看公共层内部三个关键点：

[ops_common.h:45-72](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L45-L72)

ACL 的不透明类型（`aclTensor`、`aclOpExecutor` 等）只有前置声明，构造/销毁函数也以函数指针类型 `_aclCreateTensor`、`_aclDestroyTensor` 等形式声明——因为它们的实现同样在运行期的 opapi 库里，只能 dlsym 拿地址。第 72 行 `using OpApiFunc = int (*)(void *, uint64_t, aclOpExecutor *, const aclrtStream)` 正是 aclnn 第二段「执行」接口的统一签名（u2-l5 讲过的 4 参数约定）。

[ops_common.h:209-230](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L209-L230)

`get_custom_lib_path()` 读 `ASCEND_CUSTOM_OPP_PATH` 环境变量（冒号分隔多路径），为每条路径拼上 `/op_api/lib/` 后缀。配套的 `get_default_custom_lib_path()`（第 232-275 行）则读 `ASCEND_OPP_PATH/vendors/config.ini` 的 `load_priority=` 行，解析厂商加载顺序。**这两个环境变量正是 u1-l4 里 source 算子包 `set_env.bash` 导出的**——两个单元在此闭环：不装算子包、不 source 环境，这里的列表就是空的，符号查找会一路落到官方库并最终失败。

[ops_common.h:325-379](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L325-L379)

`GetOpApiFuncAddr()` 是符号查找的总入口：先遍历 `g_custom_lib_path` 找 `libcust_opapi.so`（自定义算子优先于官方算子，防止同名覆盖），再走默认 vendors 顺序，再依次尝试 `libopapi_math.so`、`libopapi_nn.so`、`libopapi_cv.so`、`libopapi_transformer.so`、`libopapi_legacy.so`、`libopapi.so`，最后由 `GetOpApiFuncAddrFromFeatureLib`（实现在 ops_common.cpp 第 18-27 行）遍历 `libaclnn_ops_infer/train/math/sparse/fft/rand.so` 六个 feature 库。这个八级 fallback 链保证了无论算子装在哪一层都能被找到，代价是排查「调到了哪个版本的库」时要按顺序逐层看。

[ops_common.h:965-997](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L965-L997)

`EXEC_NPU_CMD_V1` 宏的开头：`static const auto getWorkspaceSizeFuncAddr = GetOpApiFuncAddr(#aclnn_api "GetWorkspaceSize")` 与 `static const auto opApiFuncAddr = GetOpApiFuncAddr(#aclnn_api)`——宏的字符串拼接 `#aclnn_api "GetWorkspaceSize"` 直接合成出两段式接口的两个名字（如 `aclnnAiInfraAggregateHiddenGetWorkspaceSize`），随后 `TORCH_CHECK` 保证两个地址都找到，否则报出「not in libopapi.so」的明确错误（这是漏装算子包时最常见的报错形态）。第 995 行 `ConvertTypes(__VA_ARGS__, workspace_size_addr, executor_addr)` 把用户参数与两个出参指针一起转换打包，第 996-997 行以 static 缓存的函数指针类型调用第一段。

[ops_common.h:1008-1024](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L1008-L1024)

宏的收尾：lambda `acl_call` 内 reinterpret_cast 成统一的 `OpApiFunc` 签名发起第二段执行，随后 `ReleaseConvertTypes` 逐个销毁 acl 中间对象；外层 `at_npu::native::OpCommand::RunOpApiV2(#aclnn_api, acl_call)` 把执行挂到当前 stream 的任务队列——异步语义由它保证。注意第 1001-1007 行：workspace 用 `at::empty(..., at::kByte)` 在 NPU 上分配，其生命周期由捕获的 `workspace_tensor` 保活。

[ops_common.cpp:13-16](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.cpp#L13-16)

全局变量的定义处：`const std::vector<std::string> g_custom_lib_path = get_custom_lib_path();`——**`.so` 被 dlopen 加载的瞬间**就会执行这两行初始化。所以「先 source 算子包环境再启动 Python」不是建议而是硬约束：环境变量在 import 时读不到，之后改环境变量也不会再生效（除非重开进程）。

#### 4.4.4 代码实践

1. **实践目标**：搞清「先装算子包、再装 wheel」这条顺序约束的证据链。
2. **操作步骤**（源码阅读型，无 NPU 可做）：
   - 在 `ops_common.h` 中 grep `ASCEND_CUSTOM_OPP_PATH` 与 `ASCEND_OPP_PATH`，确认它们只在 `get_custom_lib_path` / `get_default_custom_lib_path` 两处被读取；
   - 对照 u1-l4 安装的 vendors 目录（如 `/usr/local/Ascend/ascend-toolkit/latest/opp/vendors/omni_training_custom_transformer/`），执行 `ls <该路径>/op_api/lib/`，确认 `libcust_opapi.so` 存在；
   - 用 `nm -D <libcust_opapi.so> | grep AggregateHidden` 查看导出符号（待本地验证，容器内执行）。
3. **需要观察的现象**：`.so` 里能找到 `aclnnAiInfraAggregateHidden` 与 `aclnnAiInfraAggregateHiddenGetWorkspaceSize` 两个符号——正是 `EXEC_NPU_CMD_V1` 按名查找的两个目标。
4. **预期结果**：两个符号都存在，证明「wheel 只带调用代码，算子实现全在 run 包」的分层结论。若跳过 u1-l4 直接装 wheel，运行期 `TORCH_CHECK(getWorkspaceSizeFuncAddr != nullptr ...)` 会失败并打印 `aclnnAiInfraAggregateHidden or aclnnAiInfraAggregateHiddenGetWorkspaceSize not in libopapi.so`。

#### 4.4.5 小练习与答案

**练习 1**：`EXEC_NPU_CMD_V1` 中两处 `static const auto` 为什么必须是 static？去掉会怎样？

**答案**：static 使函数地址查找与 `ConvertToOpApiFunc` 的类型转换在**进程内只执行一次**（首次调用该算子时），后续调用直接复用缓存地址。去掉后每次调用都要走 dlopen/dlsym 的八级 fallback 链并重复做模板实例化，性能显著下降；行为仍正确但代价不可接受。

**练习 2**：aclnn 的 `aclTensor*` 是谁创建、谁销毁的？

**答案**：都在适配层完成。创建：`ConvertType(const at::Tensor&)`（ops_common.h 第 451 行起）内部 dlsym 拿 `aclCreateTensor` 把 torch 张量的 sizes/strides/dtype/存储指针打包成 aclTensor；销毁：第二段执行完成后 `ReleaseConvertTypes` 调 `aclDestroyTensor` 等逐个释放。aclnn 接口本身只消费不接管这些对象。

**练习 3**：本仓库的 `ops_common.h` 与 torch_npu 自带的同名机制是什么关系？

**答案**：include guard 表明它是从 torch_npu 的 `aten/ops/op_api/pta_common.h` 拷贝的定制副本，原理相同但独立演进。好处是不依赖 torch_npu 内部符号的稳定性、可按需增删（如本版加入了 `EXEC_NPU_CMD_v0` 旧式同步版本作为对照）；代价是 torch_npu 升级后需要人工同步上游修复——二次开发时若发现行为与官方适配层不一致，优先 diff 这份副本。

### 4.5 build_and_install.sh：三步脚本与安装产物

#### 4.5.1 概念说明

构建脚本只有 3 条有效命令，是「薄壳脚本」风格（与 u1-l4 的 `build.sh` 同一哲学：脚本只编排，不做逻辑）。它把 4.2 的 setup.py 与安装动作串起来。

#### 4.5.2 核心流程

```text
cd torch_ops_extension && bash build_and_install.sh
  ├─ ① rm -rf build            # 清理历史编译中间产物（注意：不清 dist）
  ├─ ② python3 setup.py build bdist_wheel
  │      → dist/omni_training_custom_ops-1.0-<py版本>-<py版本>-<arch>.whl
  └─ ③ cd dist && pip3 install *.whl --force-reinstall && cd -
         → 装入 site-packages，含 custom_ops_lib.so 与各算子 converter 包
```

#### 4.5.3 源码精读

[build_and_install.sh:10-21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/build_and_install.sh#L10-L21)

三条命令依次为：第 13 行 `rm -rf build` 只清理编译目录、保留 `dist`（历史 wheel 留档）；第 16 行 `python3 setup.py build bdist_wheel` 触发 4.2 节的全流程并把 wheel 产出到 `dist/`；第 19-21 行进入 `dist` 用 `pip3 install *.whl --force-reinstall` 安装——`--force-reinstall` 保证重复执行脚本时旧版本被覆盖而不是被跳过，随后 `cd -` 回原目录。

[README.md:266-273](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/README.md#L266-L273)

官方文档给出同样的命令与产物命名规则：`omni_training_custom_ops-1.0-<python_version>-<python_version>-<arch>.whl`。README 中该章节紧跟在算子 run 包安装（第 260-264 行）之后，顺序本身就是使用约束：**先 run 包（提供 libcust_opapi.so），后 wheel（提供调用代码）**。

#### 4.5.4 代码实践（本讲主实践）

**路径一：完整编译安装（需要 A2/A3 容器：已装 torch_npu + 已执行 u1-l4 的算子包安装）**

1. **实践目标**：走通「编译 wheel → 安装 → import → 列出算子」全链路。
2. **操作步骤**：
   ```bash
   # 前置：u1-l4 已完成算子包安装并 source 过 set_env.bash
   cd training/ascendc/torch_ops_extension
   bash build_and_install.sh
   # 验证安装
   python3 - <<'EOF'
   import torch, torch_npu
   import omni_training_custom_ops
   ops = [n for n in dir(torch.ops.custom) if not n.startswith('_')]
   print("torch.ops.custom 下共", len(ops), "个算子：")
   for n in sorted(ops):
       print(" ", n)
   # 验证挂载别名
   print("torch_npu 挂载检查:", hasattr(torch_npu, "npu_aggregate_hidden"))
   EOF
   ```
3. **需要观察的现象**：`dist/` 下生成 wheel 文件；python 脚本打印算子名列表，其中应包含 `npu_aggregate_hidden`、`npu_flash_attention_score_enhance`、`npu_sinkhorn`、`npu_manifold_constrained_hyper_connection_pre`、`npu_ai_infra_attention_pioneer` 等；挂载检查输出 True。
4. **预期结果**：算子名与 4.3 节 schema 清单一一对应（`torch.ops.custom` 下的名字来自 `TORCH_LIBRARY_FRAGMENT` 注册，因此**多于** pybind 绑定的 8 个）。若 import 时报 `undefined symbol` 或调用时报 `not in libopapi.so`，回到 u1-l4 检查算子包与环境变量。（本实践需真实环境，待本地验证。）

**路径二：无 NPU 环境的替代实践**

完成 4.2.4 实践 A 的 glob 清单验证 + 4.4.4 的证据链阅读，并写出路径一的完整命令与预期输出（即本讲文中的内容），标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`rm -rf build` 清理 build 但保留 dist，有什么实际影响？

**答案**：build 目录是 setuptools 的编译中间产物（`.o` 文件），清掉它强制全量重编、避免陈旧对象混入；dist 保留历史 wheel，便于回滚到旧版本安装。与 u1-l4 中 ascendc 侧「每次构建全量清理、增量靠 ccache」的策略同理，只是这里没有接 ccache。

**练习 2**：不执行脚本，直接 `pip3 install dist/xxx.whl` 与执行脚本的差别是什么？

**答案**：脚本多了「清理 + 重新编译」两步。若源码没变，直接装旧 wheel 与跑脚本结果相同；若源码变了，直接装旧 wheel 会装到过期代码——`--force-reinstall` 只保证覆盖安装，不保证重新编译。

**练习 3**：wheel 装好后，`import omni_training_custom_ops` 在没有 NPU 硬件的机器上会成功吗？

**答案**：大概率失败或部分失败：`.so` 依赖 torch_npu（可以装），`ops_common.cpp` 全局初始化只是读环境变量（不会立刻崩），但 `from . import custom_ops_lib` 加载 `.so` 时若链接的 ACL 运行库缺失则直接报 import error；即使 import 成功，首次调用算子也会在 dlsym 或 NPU 设备初始化处失败。所以「编译要 torch_npu 环境、运行要 NPU 硬件 + 算子包」是三个递进的门槛。

## 5. 综合实践

**任务：绘制并验证「一条算子调用的完整装配线」。**

以 `torch_npu.npu_aggregate_hidden(x, w)` 为对象，完成以下三件事：

1. **画调用链图**：从 Python 调用出发，标出每一跳所在的文件与行号：
   - `__init__.py` 的 setattr 别名 → `torch.ops.custom.npu_aggregate_hidden`（schema 定义在 `ops_def_registration.cpp:44`）
   - → C++ 实现函数（声明在 `function.h:47`，实现在 `npu_aggregate_hidden.cpp`，u6-l2 精读）
   - → `EXEC_NPU_CMD_V1(aclnnAiInfraAggregateHidden, ...)`（宏定义在 `ops_common.h:965`）
   - → `GetOpApiFuncAddr` 八级查找链（`ops_common.h:325-379` + `ops_common.cpp:18-27`）
   - → run 包里的 `libcust_opapi.so`（u1-l4 产物）→ tiling（u2-l3）→ kernel（u2-l4）。
2. **验证装配顺序**：写出三个产物的依赖顺序并各给一条证据（本讲已给出：README 章节顺序、`ops_common.cpp:13-14` 的 import 期初始化、`EXEC_NPU_CMD_V1` 的 TORCH_CHECK）。
3. **做一次「破坏性推演」**（只推演不执行）：如果只装 wheel 不装 run 包、或者先 import 再 source `set_env.bash`，分别会在哪一行代码处失败、报什么错？把行号和错误信息写进你的图里。

完成后再走一遍 4.5.4 的完整编译安装实践（有环境时），用实际输出核对你的推演。

## 6. 本讲小结

- `torch_ops_extension` 是算子库的 PyTorch 适配层：把 aclnn C 接口包装成 `torch.ops.custom.npu_xxx` / `torch_npu.npu_xxx`，训练脚本由此消费前五个单元讲解的算子。
- `setup.py` 用两条 glob 规则（`csrc_base/*.cpp` + `*/*/*/csrc/*.cpp`）自动收集 15 个源文件编进一个 `NpuExtension` 扩展模块 `custom_ops_lib`，新增算子目录无需改构建脚本，但必须遵守三层目录深度约定。
- `ops_def_registration.cpp` 是 schema 总账本：`TORCH_LIBRARY_FRAGMENT(custom, m)` 注册 18 条算子 schema 供 dispatcher 调用，`PYBIND11_MODULE` 另绑定 8 个 autograd 封装函数作为直调通道。
- `ops_common.h/.cpp` 是发动机：`ConvertType` 系列完成 at::Tensor ↔ aclTensor 类型转换，`GetOpApiFuncAddr` 用 dlopen/dlsym 按八级 fallback 链在运行期解析 aclnn 符号，`EXEC_NPU_CMD_V1` 宏把两段式调用 + workspace 分配 + 异步下发 + 资源释放封装成一行。
- `build_and_install.sh` 三步走（清理 → bdist_wheel → pip --force-reinstall）；产物顺序是硬约束：先装 u1-l4 的算子 run 包并 source 环境，再装 wheel，因为 `.so` 加载瞬间就要读取 `ASCEND_CUSTOM_OPP_PATH` 初始化查找路径。
- 包入口 `__init__.py` 的挂载逻辑（torch.ops.custom → torch_npu）让两种调用风格并存；mhc 家族的「csrc + converter + test 三件套」是更完整的适配范式，留给 u6-l3。

## 7. 下一步学习建议

- **u6-l2（csrc 适配层：Autograd Function 与算子注册）**：深入 `npu_aggregate_hidden.cpp` 全文，看 `torch::autograd::Function` 如何把前向/反向两个 aclnn 算子拼成可 autograd 的 torch 算子，以及 `TORCH_LIBRARY_IMPL` 对 PrivateUse1 / AutogradPrivateUse1 / Meta 三类 dispatch key 的分层注册——本讲的 schema 只是「声明」，下一讲补上「实现」。
- **u6-l3（Python converter 与适配层测试）**：看 mhc post 算子的 converter 如何做参数校验与默认值填充，理解 csrc/converter/test 三件套的完整开发范式。
- 若想巩固本讲的运行期符号解析机制，可对照 u3-l4 的 stub 桩机制——stub 是「编译期造替身」，本讲的 dlsym 是「运行期找真身」，两者互补地解决了「依赖不在我手里」的问题。
