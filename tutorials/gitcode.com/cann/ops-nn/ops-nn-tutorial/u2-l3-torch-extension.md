# PyTorch 扩展方式调用算子：torch_extension 工程

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `torch_extension/` 这个工程在 ops-nn 仓库中的定位：它把 ops-nn 算子封装成 PyTorch 可直接调用的 Python API。
2. 理解 wheel 包的构建机制：`setup.py` 如何在打包时自动「收集」散落在各算子目录下的 `torch_extension/` 子目录。
3. 掌握一次 Python 调用在四层之间的传递路径：Python API → PyTorch Dispatcher → JIT 编译的 C++ 扩展（`.so`）→ 底层 aclnn 两段式接口（上一讲 u2-l1 的内容）。
4. 能独立编译安装 wheel 包，并在 Python 里调用一个已封装算子、与 torch 原生实现对比结果。

## 2. 前置知识

阅读本讲前，建议先回顾以下概念（前几讲已建立）：

- **aclnn 两段式接口**（u2-l1）：`aclnnXxxGetWorkspaceSize` 负责校验参数、登记 executor、算 workspace；`aclnnXxx` 负责把 executor 异步提交到 stream 执行。本讲讲的 torch_extension 就是在这套接口之上再包一层 Python 外壳。
- **op_api 交付件**（u1-l3）：`<算子>/op_api/` 下的 `aclnn_*.cpp` 编译产物是 `libopapi_nn.so`（或 vendor 包的 `libcust_opapi.so`），这是 torch_extension 在运行时 `dlopen` 查找符号的目标库。
- **PyTorch Dispatcher（分发器）**：PyTorch 内部的一张「函数路由表」。每个算子先注册一个 schema（签名），再按分发键（dispatch key）注册不同后端实现。NPU 这类自定义后端使用 `PrivateUse1` 这个保留分发键；`Meta` 键则只做 shape/dtype 推导，不真正执行计算。
- **JIT 编译**：Python 包里只带 C++ 源码，第一次调用算子时才用 `ninja` 现场编译成 `.so` 并加载，而不是在打包时预编译。
- **torch_npu**：华为发布的 PyTorch NPU 适配插件，把 `PrivateUse1` 后端接到昇腾设备上，并提供 `torch_npu.utils.is_npu` 等工具函数。

一个直觉性的比喻：如果说 aclnn 是「手动挡」（用户自己构造 aclTensor、自己分两段调用），torch_extension 就是「自动挡」——用户拿到的是普通的 `torch.Tensor`，转换、workspace、流调度全部由封装层代劳。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `torch_extension/README.md` | torch_extension 的构建、安装、目录结构、开发指南入口 |
| `torch_extension/setup.py` | wheel 打包配置：自动收集各算子的 torch_extension 文件、生成 entry point、支持整包/子包 |
| `torch_extension/cann_ops_nn/__init__.py` | 包入口：导入 ops 并把算子提升为包级属性（`cann_ops_nn.swiglu_group`） |
| `torch_extension/cann_ops_nn/ops/__init__.py` | 算子自动发现：先扫包目录，再叠加 entry point（子包优先） |
| `torch_extension/cann_ops_nn/op_builder/builder.py` | `OpBuilder` 基类：schema/Meta 注册、头文件与链接参数拼装、JIT 编译缓存 |
| `torch_extension/cann_ops_nn/common/aclnn_common.h` | C++ 侧公共头：`ACLNN_CMD` 宏、at::Tensor→aclTensor 类型转换、算子库符号查找 |
| `activation/swiglu_group/torch_extension/swiglu_group.py` | 算子 Python 前端示例：schema、Meta、PrivateUse1 实现、autograd 注册 |
| `activation/swiglu_group/torch_extension/csrc/swiglu_group.cpp` | 算子 C++ 后端示例：校验、建输出、调用 ACLNN_CMD |
| `docs/zh/develop/torch_extension_develop_guide.md` | 官方开发指南（另一种「单文件四合一」的 experimental 开发路线） |
| `activation/gelu/op_api/aclnn_gelu.cpp` | 底层 aclnn 两段式接口参照（对应 u2-l1 讲过的内容） |

注意一个重要事实：**并非仓库里每个算子都有 torch_extension 封装**。截至当前 HEAD，带 `<算子>/torch_extension/` 目录的算子只有十余个（如 `activation/swiglu_group`、`norm/rms_norm_dynamic_quant`、`matmul/matmul_emu_split_weight`、`quant/flat_quant` 等）。`activation/gelu` 没有 torch_extension 目录——本讲引用它的 op_api 源码，是为了说明封装层底下对接的正是 u2-l1 分析过的那套 aclnn 接口。

## 4. 核心概念与源码讲解

### 4.1 torch_extension 工程总览：目录组织与整包/子包机制

#### 4.1.1 概念说明

torch_extension 解决的问题是「业务集成」：aclnn 调用需要写 C++ 样例、手动管理 aclTensor；GE 图模式需要构图（u2-l2）。而绝大多数算法工程师的日常工作环境是 PyTorch——他们希望直接写 `y = cann_ops_nn.swiglu_group(x)`。

为此，仓库提供了一个固定骨架 `torch_extension/cann_ops_nn/`（包的「公共部分」），而每个算子的封装文件则**放在算子自己的目录里**（如 `activation/swiglu_group/torch_extension/`），打包时才被收集进 wheel。这带来两个好处：

1. 算子的 C++ kernel、op_host、op_api、torch_extension 封装同居一个目录，改一个算子只看一处。
2. 包骨架与算子数量解耦，新增算子不需要改 `cann_ops_nn/` 下的任何公共文件。

#### 4.1.2 核心流程

wheel 包有两种形态，由是否指定 `--ops` 决定：

```
bash build.sh --torch_extension                          → 整包 cann_ops_nn（全部算子）
bash build.sh --torch_extension --ops=swiglu_group        → 子包 cann_ops_nn_custom（指定算子）
                                        --vendor_name=custom
bash build.sh --torch_extension --experimental            → 仅 experimental 目录下的算子
```

整包与子包安装目录物理隔离（`cann_ops_nn/` 与 `cann_ops_nn_<vendor>/`），可共存；子包通过 entry point 注册算子，**优先级高于整包**，卸载子包后整包同名算子自动接管。这套机制支持「我只改了一个算子，就只发一个小包覆盖」的迭代方式。

#### 4.1.3 源码精读

README 的目录结构图说明了「包骨架在 `torch_extension/`、算子文件在 `<category>/<op>/torch_extension/`」的两地布局：

[torch_extension/README.md:100-123](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/README.md#L100-L123) —— 官方目录结构图：`cann_ops_nn/` 下是 op_builder、common、csrc、ops 四个公共子目录；每个算子在仓库根下的 `<category>/<op>/torch_extension/` 中放 `<op>.py`（Python 前端）和 `csrc/<op>.cpp`（C++ 桥接）。

[torch_extension/README.md:21-33](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/README.md#L21-L33) —— 三种构建命令：整包、单算子包、多算子包（`--ops=op1,op2`）。注意 `--torch_extension` 只构建 wheel，**不执行 cmake 编译**——算子本身的 `libopapi_nn.so` 仍需按 u1-l2 的 `--pkg` 流程另行编译安装。

[torch_extension/README.md:127-142](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/README.md#L127-L142) —— 快速入门代码：`import cann_ops_nn` 后即可用 `cann_ops_nn.swiglu_group(x)` 或等价的 `cann_ops_nn.ops.swiglu_group(x)`，输入输出都是普通 NPU 上的 `torch.Tensor`。

[torch_extension/README.md:259-274](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/README.md#L259-L274) —— 运行期算子库查找顺序：先找 vendor 包的 `libcust_opapi.so`（沿 `ASCEND_CUSTOM_OPP_PATH` 与 `LD_LIBRARY_PATH`），找不到再退回仓库构建安装的 `libopapi_nn.so`。这与 u1-l2 讲过的自定义算子包安装位置（`opp/vendors/<vendor>_nn`）直接衔接。

#### 4.1.4 代码实践

1. **实践目标**：确认本机环境是否具备构建 torch_extension 的前置条件，并盘点仓库中哪些算子已有封装。
2. **操作步骤**：
   - 检查依赖：`python3 -c "import torch, torch_npu; print(torch.__version__)"`，并用 `pip list | grep ninja` 确认 ninja 已装（README 前置条件：Python 3.8+、GCC 9.4+、PyTorch≥2.6.0、匹配版 torch_npu）。
   - 在仓库根目录执行 `find . -maxdepth 4 -type d -name torch_extension | grep -v torch_extension/cann | sort`，列出所有算子封装目录。
3. **需要观察的现象**：find 输出的算子列表；torch/torch_npu 版本号打印。
4. **预期结果**：得到十余个算子目录（activation/swiglu_group、quant/flat_quant、norm/rms_norm_dynamic_quant 等）。torch 版本低于 2.6.0 则需先升级环境。本步骤只做环境盘点，不涉及 NPU 计算，可在任意 Linux 机器执行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--torch_extension` 构建 wheel 时不需要指定 `--soc`（NPU 架构），而 `--pkg` 编译算子包需要？

**答案**：wheel 包里只有 Python 文件和 C++ **源码**，真正的 NPU 编译发生在运行期 JIT（第一次调用时）以及底层 `libopapi_nn.so` 中；后者才是携带芯片二进制的交付件，须由 `--pkg --soc=...` 预先编译安装。所以 torch_extension 的 wheel 与芯片架构解耦。

**练习 2**：整包 `cann_ops_nn` 和子包 `cann_ops_nn_custom` 同时安装且都含 `swiglu_group`，调用 `cann_ops_nn.swiglu_group(x)` 时执行哪个？

**答案**：执行子包的版本。子包通过 entry point（group 为 `cann_ops_nn.ops`）注册算子，加载时覆盖同名的目录发现结果；卸载子包后整包实现自动恢复。

### 4.2 setup.py 打包机制：自动收集算子与 entry point 生成

#### 4.2.1 概念说明

`setup.py` 是 Python 打包的标准入口。它的特别之处在于：wheel 里绝大部分内容（算子的 `.py` 和 `.cpp`）**不在 `torch_extension/` 目录下，而是散落在整个仓库**。setup.py 因此承担了一个「收集器」的角色——遍历仓库，把所有 `<category>/<op>/torch_extension/` 里的文件搬运（stage）到 wheel 的标准位置 `cann_ops_nn/ops/<category>/<op>/` 与 `cann_ops_nn/csrc/`。

理解这段代码对后续「新增算子封装」很重要：开发者不需要注册任何清单，只要按约定建目录，打包就会自动带上。

#### 4.2.2 核心流程

```
读取环境变量 TORCH_EXTENSION_OPS / TORCH_EXTENSION_VENDOR / TORCH_EXTENSION_EXPERIMENTAL
        │
        ▼
遍历仓库根的每个大类目录（两级 <cat>/<op>/torch_extension 与三级 experimental/<cat>/<op>/torch_extension）
        │  按算子名过滤（若指定了 --ops）
        ▼
收集 <op>.py、__init__.py、可选的 graph_convert_<op>.py → _op_py_files
收集 csrc/<op>.cpp                                      → _op_cpp_files（重名冲突直接报错）
        │
        ▼
build_py 阶段：把收集的文件复制进 build_lib；
  - 整包：用大类自己的 __init__.py
  - 子包：生成 entry point（"op = 包.ops.cat.op:op"）并改写 import 前缀
```

#### 4.2.3 源码精读

[torch_extension/setup.py:36-51](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/setup.py#L36-L51) —— 从 `TORCH_EXTENSION_OPS`/`TORCH_EXTENSION_VENDOR` 环境变量读取 build.sh 透传的参数，决定包名：指定了算子列表就构建子包 `cann_ops_nn_<vendor>`，否则是整包 `cann_ops_nn`。

[torch_extension/setup.py:143-179](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/setup.py#L143-L179) —— 收集器的主体循环：先按两级路径 `<cat>/<name>/torch_extension` 查找，再对 experimental 等三级结构按 `<cat>/<subcat>/<op>/torch_extension` 查找；命中后调用 `_collect_op` 登记文件。experimental 目录只在 `--experimental` 时纳入，反之非 experimental 目录在 `--experimental` 模式下被跳过（`setup.py:148-152`）。

[torch_extension/setup.py:123-140](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/setup.py#L123-L140) —— `_collect_op`：收集 `__init__.py`、`<op>.py`、可选的 `graph_convert_<op>.py`，并经 `_collect_cpp` 收集 `csrc/` 下的 `.cpp`。若两个算子的 csrc 文件重名会直接 `raise ValueError`，这就是所有 csrc 汇集到同一目录 `cann_ops_nn/csrc/` 的冲突保护。

[torch_extension/setup.py:217-222](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/setup.py#L217-L222) —— 子包模式下为每个算子生成 entry point 字符串 `"<op> = <包名>.ops.<cat>.<op>:<op>"`，最终在 `setup()` 的 `entry_points={"cann_ops_nn.ops": ...}`（`setup.py:403`）注册。这正是 4.1 中「子包优先」机制的落地。

[torch_extension/setup.py:247-334](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/setup.py#L247-L334) —— 自定义 `BuildPyWithOps.run()`：子包模式下先删掉 build 目录里未选中的算子，再生成只含选中算子的分类 `__init__.py`；复制 `.py` 时还会把 `from cann_ops_nn.op_builder` 改写成 `from cann_ops_nn_<vendor>.op_builder`（`setup.py:307-319`），保证子包不依赖整包。

[torch_extension/setup.py:389-404](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/setup.py#L389-L404) —— 最终的 `setup()` 调用：`install_requires` 只有 ninja/torch/torch_npu 三个运行期依赖；`package_data` 用 `_non_python_files` 把 `.h`/`.cpp` 等非 Python 文件一并打入 wheel（JIT 编译需要源码随包分发）。

#### 4.2.4 代码实践

1. **实践目标**：验证「不写清单、建目录即被收集」的机制。
2. **操作步骤**：
   - 只读实验：在仓库根执行 `python3 -c`，模拟 setup.py 的遍历逻辑（示例代码，不修改仓库）：

     ```python
     import os
     root = "."
     for cat in sorted(os.listdir(root)):
         cat_path = os.path.join(root, cat)
         if not os.path.isdir(cat_path) or cat.startswith((".", "_")):
             continue
         for name in sorted(os.listdir(cat_path)):
             te = os.path.join(cat_path, name, "torch_extension")
             if os.path.isdir(te):
                 print(cat, name)
     ```

   - 再执行 `TORCH_EXTENSION_OPS=swiglu_group python3 setup.py --name` 观察包名输出。
3. **需要观察的现象**：第一段脚本打印的 (大类, 算子) 二元组列表；第二段命令输出的包名。
4. **预期结果**：列表与 4.1.4 中 find 的结果一致；第二段输出 `cann_ops_nn_custom`（指定了 ops 且未设 vendor 时默认 custom）。若本机缺 setuptools 则命令报 ImportError，属环境问题。实际 wheel 构建请在配套 NPU 环境执行，本机观察为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果两个不同大类下各有一个算子都叫 `foo`，且都有 `csrc/foo.cpp`，打包会发生什么？

**答案**：`_collect_cpp`（setup.py:106-120）检测到目标路径 `csrc/foo.cpp` 重复，直接抛出 `ValueError`。因为所有算子的 csrc 都摊平到同一个 `cann_ops_nn/csrc/` 目录，文件名即命名空间。

**练习 2**：为什么子包要在复制 `.py` 时改写 `from cann_ops_nn.op_builder` 为 `from cann_ops_nn_<vendor>.op_builder`？

**答案**：为了物理隔离。子包安装目录是 `cann_ops_nn_<vendor>/`，如果其算子代码仍 import 整包的 `cann_ops_nn.op_builder`，那么没装整包时子包无法工作；改写后子包自带一份完整的 builder，二者可独立安装、互不依赖。

### 4.3 Python 前端：OpBuilder 与 PyTorch Dispatcher 注册

#### 4.3.1 概念说明

每个算子封装由两部分组成：Python 前端（`<op>.py`）和 C++ 桥接（`csrc/<op>.cpp`）。前端的职责是把这个算子「登记」进 PyTorch 的分发体系：

- **schema**：一条函数签名字符串，如 `swiglu_group(Tensor x, Tensor? weight=None, ...) -> Tensor`，相当于向 Dispatcher 申报「世界上存在这个算子」。
- **Meta 实现**：只根据输入推导输出 shape/dtype，不碰 NPU。有了它，`torch.compile`、FakeTensor、导出 tracing 等功能才能工作。
- **PrivateUse1 实现**：真正的 NPU 执行路径——触发 JIT 编译加载 `.so`，再调用其中的 pybind11 函数。

公共骨架被抽成了 `OpBuilder` 抽象基类，位于 `torch_extension/cann_ops_nn/op_builder/builder.py`。

#### 4.3.2 核心流程

一次 `cann_ops_nn.swiglu_group(x)` 调用的完整路径：

```
import cann_ops_nn
  → ops/__init__.py 扫描目录 + entry point，逐个 import 算子模块
  → 模块 import 时即实例化 Builder 并 _ensure_initialized()：
       注册 schema（torch.library.define）+ 注册 Meta 实现
  → 包级 __init__.py 把算子函数提升为 cann_ops_nn.swiglu_group
调用 swiglu_group(x)
  → Dispatcher 按 PrivateUse1 键路由到 @impl 注册的 Python 函数
  → builder.load()：首次调用则用 ninja JIT 编译 csrc/*.cpp 为 .so（结果缓存在 OpBuilder._loaded_ops）
  → 调用 .so 里的 pybind11 函数 swiglu_group（进入 4.4 的 C++ 桥接层）
```

#### 4.3.3 源码精读

[torch_extension/cann_ops_nn/ops/__init__.py:21-36](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/cann_ops_nn/ops/__init__.py#L21-L36) —— `_discover_ops_from_entry_points`：读取 entry point group `cann_ops_nn.ops`（子包注册的算子）。

[torch_extension/cann_ops_nn/ops/__init__.py:39-57](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/cann_ops_nn/ops/__init__.py#L39-L57) —— `_discover_ops_from_dir`：扫描包内 `ops/<cat>/<op>/` 目录发现算子；`_load_op` 负责实际 import 并把算子函数挂到模块全局名空间，还会顺带加载可选的 `graph_convert_<op>.py`（图回退转换，服务于图模式）。

[torch_extension/cann_ops_nn/ops/__init__.py:86-90](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/cann_ops_nn/ops/__init__.py#L86-L90) —— 合并策略：`_all_ops = dict(_dir_ops)` 后用 `_ep_ops` **update 覆盖**——entry point（子包）优先于目录发现（整包），这就是「子包覆盖整包」的代码级实现。

[torch_extension/cann_ops_nn/__init__.py:22-30](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/cann_ops_nn/__init__.py#L22-L30) —— 包入口先 `import torch_npu`（同时把 ops 一并导入触发全部注册），再把 `ops` 下非下划线属性提升为包级名字，于是 `cann_ops_nn.swiglu_group` 与 `cann_ops_nn.ops.swiglu_group` 等价（README 快速入门中两种写法的来源）。

[torch_extension/cann_ops_nn/op_builder/builder.py:23-30](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/cann_ops_nn/op_builder/builder.py#L23-L30) —— `get_as_library`：用 `torch.library.Library("cann_ops_nn", "DEF")` 创建算子命名空间（DEF 失败则退回 FRAGMENT），后续所有 schema 都定义在这个库里，对应调用名 `torch.ops.cann_ops_nn.<op>`。

[torch_extension/cann_ops_nn/op_builder/builder.py:46-57](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/cann_ops_nn/op_builder/builder.py#L46-L57) —— `_ensure_initialized`：首次调用时定位 torch_npu 与 CANN 安装路径，若 `torch.ops.cann_ops_nn` 上还没有该算子则注册 schema 和 Meta。模块 import 时就会执行它，所以「装包即注册」。

[torch_extension/cann_ops_nn/op_builder/builder.py:113-131](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/cann_ops_nn/op_builder/builder.py#L113-L131) —— `include_paths`：JIT 编译的头文件搜索路径，包含三处关键位置——vendor 包的 `op_api/include`（自定义算子的 aclnn 头文件）、CANN 的 `include` 与 `include/aclnnop`（内置 aclnn 头文件，`aclnn_new_operator.h` 就从这里解析）、以及包内 `common/`（`aclnn_common.h`）。

[torch_extension/cann_ops_nn/op_builder/builder.py:152-167](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/cann_ops_nn/op_builder/builder.py#L152-L167) —— `extra_ldflags`：链接 `-lascendcl -ltorch_npu`，并优先把 vendor 包的 `op_api/lib` 加入 `-L`——再次呼应 u1-l2 的 `LD_LIBRARY_PATH` 配置。

[torch_extension/cann_ops_nn/op_builder/builder.py:169-200](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/cann_ops_nn/op_builder/builder.py#L169-L200) —— `load()`：以算子名为键查 `_loaded_ops` 缓存，未命中则检查 ninja、调用 `torch.utils.cpp_extension.load` 做 JIT 编译；失败时给出三条常见原因（未 source CANN 环境、缺 gcc、缺 ninja）。编译结果按算子名全局缓存，只有第一次调用慢。

[activation/swiglu_group/torch_extension/swiglu_group.py:17-25](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/swiglu_group/torch_extension/swiglu_group.py#L17-L25) —— `_swiglu_shape`：Python 版输出 shape 推导（最后一维减半），供 Meta 使用。

[activation/swiglu_group/torch_extension/swiglu_group.py:28-48](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/swiglu_group/torch_extension/swiglu_group.py#L28-L48) —— `SwigluGroupOpBuilder`：三个必须实现的抽象方法齐备——`sources`（C++ 源码相对路径）、`schema`（含可选参数与 keyword-only `clamp_limit` 的签名）、`register_meta`（用 `@impl(..., "Meta")` 注册 shape 推导）。第 47-48 行在 import 时实例化并初始化。

[activation/swiglu_group/torch_extension/swiglu_group.py:51-54](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/swiglu_group/torch_extension/swiglu_group.py#L51-L54) —— PrivateUse1 实现：`builder.load()` 拿到 JIT 编译的模块后转发调用。这就是 Python 世界与 C++ 世界的交界点。

[activation/swiglu_group/torch_extension/swiglu_group.py:57-88](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/swiglu_group/torch_extension/swiglu_group.py#L57-L88) —— 加分项：`torch.library.register_autograd` 注册反向传播。前向走 NPU 算子，反向调用 `swiglu_group_backward` 算子，`setup_context` 负责保存反向所需张量。封装层因此能融入 `torch.autograd` 训练流程——这是裸调 aclnn 没有的能力。

#### 4.3.4 代码实践

1. **实践目标**：不改仓库代码，只通过阅读源码画出「import 到可调用」的注册时序。
2. **操作步骤**：
   - 依次阅读 `cann_ops_nn/__init__.py` → `ops/__init__.py` 的 `_load_op` → `swiglu_group.py` 的模块级语句（47-48 行）→ `builder.py` 的 `_ensure_initialized`。
   - 用纸或文本画出：import 链上每一步发生了什么、哪些步骤在 import 期、哪些推迟到首次调用。
3. **需要观察的现象**：你的时序图中应能明确区分「import 期完成」与「首次调用期完成」两类动作。
4. **预期结果**：import 期完成 schema 注册 + Meta 注册；首次调用完成 JIT 编译 + `.so` 加载 + 进入 C++ 桥接。纯源码阅读，无需 NPU 环境。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Meta 实现是必须的？删掉它 `swiglu_group(x)` 还能跑吗？

**答案**：直接在真 NPU 张量上调用仍能跑（走 PrivateUse1 实现），但 `torch.compile`、FakeTensor、`torch.export` 等需要在没有真实设备数据的情况下推导输出 shape 的功能会失败。Meta 实现就是给这些「只看元数据」的场景用的。

**练习 2**：`OpBuilder._loaded_ops` 是类级别字典，为什么设计成全局缓存而不是每个实例自己缓存？

**答案**：同一个算子可能被多个模块/子包分别实例化 Builder，类级别字典保证整个进程里一个算子只 JIT 编译加载一次，`.so` 也只 dlopen 一份，避免重复编译开销和符号重复注册。

### 4.4 C++ 桥接层：ACLNN_CMD 宏与 aclnn 两段式的对应

#### 4.4.1 概念说明

C++ 桥接文件（`csrc/<op>.cpp`）是 PyTorch 张量世界与 aclnn 世界的翻译官。它做四件事：设备检查、构造输出张量、类型转换、调用 aclnn。其中最「重」的部分——类型转换和两段式调用——被 macros 化到公共头 `aclnn_common.h` 的 `ACLNN_CMD` 宏里，每个算子的 cpp 因此可以非常短（swiglu_group.cpp 全文只有 63 行）。

这里正是与 u2-l1 的衔接点：`ACLNN_CMD(aclnnSwigluGroup, ...)` 宏展开后，就是对 `aclnnSwigluGroupGetWorkspaceSize` 和 `aclnnSwigluGroup` 两个函数的先后调用——与手写 aclnn 样例（如 `test_aclnn_gelu.cpp` 风格）调用 `aclnnGeluGetWorkspaceSize`/`aclnnGelu`（[activation/gelu/op_api/aclnn_gelu.cpp:89-131](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L89-L131)）是同一套机制，只是参数构造和资源释放全部自动化了。

#### 4.4.2 核心流程

`ACLNN_CMD(aclnn_api, args...)` 展开后的执行序列：

```
1. DecodeDevice(args)：从参数中找第一个 at::Tensor，确定 NPU 设备并加 DeviceGuard
2. GetOpApiFuncAddr("aclnn_apiGetWorkspaceSize") / GetOpApiFuncAddr("aclnn_api")
     → 先在 libcust_opapi.so（vendor 包）里 dlsym，再退回 libopapi_nn.so
3. ConvertTypes(args...)：逐参数重载转换
     at::Tensor → aclTensor*（dims/stride/format/storage 全量搬运）
     at::Scalar → aclScalar*，IntArrayRef → aclIntArray*，标量原样透传
4. 调用第一段 aclnn_apiGetWorkspaceSize(..., &workspace_size, &executor)
5. 若 workspace_size > 0，用 at::empty 在 NPU 上分配 workspace（替代手写样例的 aclrtMalloc）
6. 构造 acl_call 闭包（调用第二段 aclnn_api + 释放所有 acl* 描述符），
   交给 at_npu::native::OpCommand 在当前 NPU stream 上异步执行
```

对比 u2-l1 手写样例的七步骨架：步骤 3 对应「构造 aclTensor」，步骤 4-6 对应「两段式调用 + workspace + stream」，而「同步、拷回验证」由 PyTorch 自身的 tensor 语义接管（Python 侧拿到 `y` 后 `.cpu()` 时自动同步）。

#### 4.4.3 源码精读

[activation/swiglu_group/torch_extension/csrc/swiglu_group.cpp:19-41](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/swiglu_group/torch_extension/csrc/swiglu_group.cpp#L19-L41) —— 算子级校验与输出 shape 推导：`CheckNpuTensor` 用 `torch_npu::utils::is_npu` 确认张量在 NPU 上；`GetSwigluOutputShape` 是 C++ 版的「最后一维减半」，与 Python 侧 `_swiglu_shape` 逻辑一致——两侧各推导一次，Meta 给框架用，C++ 给真实分配用。

[activation/swiglu_group/torch_extension/csrc/swiglu_group.cpp:44-55](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/swiglu_group/torch_extension/csrc/swiglu_group.cpp#L44-L55) —— 算子主体：校验 → `at::empty` 分配输出 → `ACLNN_CMD(aclnnSwigluGroup, x, weight, group_index, clamp_limit, y)` 一行完成 aclnn 调用 → 返回 `y`。第 60-63 行的 `PYBIND11_MODULE` 把这个函数暴露为 Python 模块级函数，供 4.3 中 `op_module.swiglu_group(...)` 调用。

[torch_extension/cann_ops_nn/common/aclnn_common.h:476-505](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/cann_ops_nn/common/aclnn_common.h#L476-L505) —— `ConvertType(const at::Tensor&)` 重载：把 `at::ScalarType` 映射为 `aclDataType`（映射表在 82-135 行），再从 `at::Tensor` 抽取 sizes/strides/storage_offset/format/storage_dims 调用 `aclCreateTensor`——即 u2-l1 手写的「构造 aclTensor 描述符」步骤的全自动版。

[torch_extension/cann_ops_nn/common/aclnn_common.h:352-371](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/cann_ops_nn/common/aclnn_common.h#L352-L371) —— `GetOpApiFuncAddr`：符号查找的两级 fallback——先遍历 `libcust_opapi.so` 的所有已加载句柄（vendor 自定义算子包），命中即返回；否则在 `libopapi_nn.so` 里找。这解释了为什么安装了 vendor 子包后同名算子会被优先使用。

[torch_extension/cann_ops_nn/common/aclnn_common.h:1002-1030](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/cann_ops_nn/common/aclnn_common.h#L1002-L1030) —— `ACLNN_CMD` 宏的前半段：设备守卫、`static` 缓存两个函数地址（每个算子只 dlsym 一次）、设置确定性计算标志（读取 `at::globalContext().deterministicAlgorithms()` 并经 `aclrtCtxSetSysParamOpt` 传入 CANN 运行时，对应 README「确定性计算」一节）、`ConvertTypes` 转换参数后调用第一段 `GetWorkspaceSize`。

[torch_extension/cann_ops_nn/common/aclnn_common.h:1031-1057](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/torch_extension/cann_ops_nn/common/aclnn_common.h#L1031-L1057) —— `ACLNN_CMD` 宏的后半段：workspace 非空时用 `at::empty({workspace_size}, kByte)` 在 NPU 上分配（对比手写样例的 aclrtMalloc/aclrtFree，这里交给 PyTorch 内存池，无泄漏风险）；第二段调用与描述符释放（`ReleaseConvertTypes`）被封装进 `acl_call` 闭包，交给 `OpCommand` 在当前 stream 异步执行。

[activation/gelu/op_api/aclnn_gelu.cpp:89-131](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/gelu/op_api/aclnn_gelu.cpp#L89-L131) —— 底层参照：`aclnnGeluGetWorkspaceSize` 与 `aclnnGelu` 的两段式实现（u2-l1 已精读）。`ACLNN_CMD` 宏最终 dlsym 到的正是这类函数。gelu 虽无 torch_extension 封装，但任何新封装都是对同结构 aclnn 函数的桥接。

[docs/zh/develop/torch_extension_develop_guide.md:40-46](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/develop/torch_extension_develop_guide.md#L40-L46) —— 官方指南总结的算子实现四要素：Schema 注册、Meta 实现（InferShape+InferDtype）、Kernel 实现、NPU 调用实现。本讲 4.3/4.4 的 Python+C++ 双文件结构是该思想在 `torch_extension/` 体系下的工程化形态（指南正文展示的是 experimental 目录下「单文件四合一」的另一条路线，可对照阅读）。

#### 4.4.4 代码实践

1. **实践目标**：数清 `ACLNN_CMD` 相比手写 aclnn 样例省掉了哪些步骤。
2. **操作步骤**：
   - 打开 [examples/add_example/examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann-ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/examples/test_aclnn_add_example.cpp)（u1-l4/u2-l1 用过的样例）与本讲的 `swiglu_group.cpp`，逐行对照。
   - 列出样例中所有「描述符构造/销毁、workspace malloc/free、stream 同步」语句，确认它们在 swiglu_group.cpp 中均不存在，并标注对应到 aclnn_common.h 的哪一行宏逻辑。
3. **需要观察的现象**：一张两列对照表：左列手写步骤，右列宏内自动化位置。
4. **预期结果**：手写样例约七步（构造 aclTensor → GetWorkspaceSize → malloc → 执行 → 同步 → 拷回 → 释放），其中前五步的机械部分在宏内完成，同步与拷回由 PyTorch tensor 语义隐式处理。纯源码阅读实践。

#### 4.4.5 小练习与答案

**练习 1**：`ACLNN_CMD` 里两个函数地址为什么用 `static const auto` 缓存？如果不缓存会有什么代价？

**答案**：`static` 使 dlsym 只在首次执行时发生，之后直接用地址。不缓存则每次算子调用都要走 `GetOpApiFuncAddr` 的 dlsym 查找（还要构造字符串拼接），在深度学习训练中算子每步都被调用千百次，累积开销可观。

**练习 2**：为什么 workspace 用 `at::empty` 分配而不是 `aclrtMalloc`？

**答案**：`at::empty` 走 torch_npu 的 NPU 内存池，由 `workspace_tensor` 这个 at::Tensor 持有；闭包执行完毕后张量离开作用域自动归还内存池。`aclrtMalloc` 则需要手动配对 `aclrtFree`，漏配即泄漏。宏把 workspace 生命周期绑定到 C++ 栈对象，实现了异常安全的自动管理。

**练习 3**：`swiglu_group.cpp` 的 `GetSwigluOutputShape` 和 `swiglu_group.py` 的 `_swiglu_shape` 是重复逻辑吗？能否只留一份？

**答案**：逻辑重复但服务对象不同：Python 版给 Meta（框架侧 shape 推导，如 torch.compile），C++ 版给真实执行时的输出分配。理论上可让 C++ 侧信任 Meta 的结果，但保留双侧校验更稳妥（防止绕过 Dispatcher 的直接调用）；这是封装层常见的「推导逻辑双语实现」代价。

## 5. 综合实践

**任务：编译 torch_extension 子包，在 Python 中调用 swiglu_group 并与 torch 原生实现对账。**

前置：配套 NPU 环境，已完成 u1-l2 的算子包编译安装（`libopapi_nn.so` 可被找到），Python 3.8+、PyTorch≥2.6.0、匹配版 torch_npu、ninja。

1. 构建子包（只含 swiglu_group 一个算子，构建快）：

   ```sh
   cd <仓库根>
   bash build.sh --torch_extension --ops=swiglu_group --vendor_name=custom
   ```

2. 安装 wheel 并确认 entry point 生效：

   ```sh
   python3 -m pip install build_out/cann_ops_nn_custom-*.whl
   python3 -c "from importlib.metadata import entry_points; \
     print([ep for ep in entry_points(group='cann_ops_nn.ops')])"
   ```

3. 编写对账脚本 `test_swiglu_group.py`（示例代码，非仓库原有文件）：

   ```python
   import torch
   import torch_npu
   import cann_ops_nn

   torch.manual_seed(0)
   x = torch.randn(16, 128, dtype=torch.float16).npu()

   y = cann_ops_nn.swiglu_group(x)          # NPU 算子

   # torch 原生参考实现：x 沿最后一维对半分，前半过 SiLU 再乘后半
   a, b = x.float().chunk(2, dim=-1)
   y_ref = (torch.nn.functional.silu(a) * b).to(torch.float16)

   print("shape:", y.shape, "expect:", torch.Size([16, 64]))
   print("allclose:", torch.allclose(y.float().cpu(), y_ref.float().cpu(), atol=1e-2))
   ```

   > swiglu 的语义（SwiGLU 分组激活）请以 [activation/swiglu_group](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/activation/swiglu_group/README.md) 的算子 README 为准；若参考实现公式与算子定义有出入（如 clamp_limit、group 维度），请先阅读算子 README 校准参考实现再对比。

4. 观察三个现象：
   - 首次执行 `swiglu_group` 会有明显的 JIT 编译等待（ninja 编译 `.so`），第二次执行明显变快——验证 `OpBuilder._loaded_ops` 缓存。
   - 输出 shape 是否为 `[16, 64]`（最后一维减半）。
   - 精度对比结果（float16 下建议 `atol=1e-2` 量级）。
5. 进阶（可选）：`pip uninstall cann-ops-nn-custom` 后安装整包 `cann_ops_nn-*.whl`，重复第 3 步，验证整包接管同名算子。

本实践在无 NPU 环境下无法完成第 3 步之后的验证，实际运行结果「待本地验证」。

## 6. 本讲小结

- torch_extension 把 ops-nn 算子封装成 `cann_ops_nn.<op>(x)` 形式的 PyTorch API，是三种调用方式（aclnn eager、GE 图模式、PyTorch 扩展）中最贴近算法工程师日常的一种。
- 工程采用「包骨架 + 算子散置」布局：公共骨架在 `torch_extension/cann_ops_nn/`，每个算子的封装放在自己的 `<category>/<op>/torch_extension/`，`setup.py` 打包时自动收集，新增算子零注册。
- 整包与子包（`--ops` + `--vendor_name`）物理隔离、可共存，子包经 entry point 优先覆盖整包，支持单算子快速迭代发布。
- Python 前端经 `OpBuilder` 向 PyTorch Dispatcher 注册 schema、Meta（shape 推导）与 PrivateUse1（NPU 执行）三层；首次调用触发 ninja JIT 编译并全局缓存。
- C++ 桥接层的 `ACLNN_CMD` 宏把 u2-l1 手写的 aclnn 两段式调用全自动化：at::Tensor→aclTensor 转换、workspace 内存池分配、stream 异步执行、描述符释放、确定性计算标志传递。
- 封装层还能注册 autograd 反向（swiglu_group 示例），使自定义算子融入 `torch.autograd` 训练——这是裸调 aclnn 不具备的能力。

## 7. 下一步学习建议

本讲完成了「算子调用方式」单元（u2）的最后一讲。接下来进入 u3「算子定义与 Shape 推导」，建议：

1. 学习 u3-l1（OpDef 算子原型定义），理解 `op_host/*_def.cpp` 如何声明算子支持的 dtype/format——这些声明决定了 aclnn 头文件注释中的参数约束，也就是本讲 `ACLNN_CMD` 调用失败时报错的源头。
2. 学习 u3-l2（Infershape），对比本讲的 Meta 实现：`register_meta` 是 Python 侧 shape 推导，`*_infershape.cpp` 是 CANN 侧 shape 推导，二者概念同源。
3. 想给没有封装的算子（如 gelu）补一个 torch_extension 的读者，可按 `torch_extension/README.md` 开发者指南一节（146-234 行）的模板动手，同时参考 `torch_extension/cann_ops_nn/docs/torch_extension_guidelines.md` 开发规范；这与 u9-l3 的贡献流程衔接。
4. 继续阅读源码的顺序建议：先重读 `builder.py` 全文（200 行，本讲只精读了骨架），再看 `experimental/quant/turbo_quant_compress_latent/torch_extension/` 等含可选 `graph_convert_*.py` 的算子，理解图回退转换与 u2-l2 图模式的关系。
