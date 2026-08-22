# torch_ops_extension 快速上手：在 PyTorch 中调用自定义算子

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立执行 `build_and_install.sh`，把 `torch_ops_extension` 编译并安装成 `omni_custom_ops` wheel 包，说清楚 `setup.py` 里每个关键步骤（源码收集、NpuExtension、package_data）在做什么。
2. 理解「方向盘」的安装原理：C++ 扩展 `custom_ops_lib` 如何把算子注册进 `torch.ops.custom` 命名空间，`__init__.py` 又如何把这些算子镜像挂载到 `torch_npu` 模块上。
3. 用 `torch.ops.custom.npu_xxx(...)` 与 `torch_npu.npu_xxx(...)` 两种等价写法调用自定义推理算子，并能仿照仓库自带 example 写出调用一个新算子的最小脚本。

本讲是第 1 单元的收尾：u1-l2 装好了「发动机」（run 包装进 CANN vendors），u1-l3 认识了算子目录五件套，本讲把「方向盘」（wheel 包）装上，让 PyTorch 用户能真正开起来。

## 2. 前置知识

- **wheel 包**：Python 官方的二进制分发格式（一个 `.whl` 压缩包），用 `pip install xxx.whl` 安装。它既可以包含纯 Python 代码，也可以包含编译好的 C++ 扩展（`.so` 文件）。
- **Python C++ 扩展**：用 C++ 写、编译成 `.so` 共享库、在 Python 里 `import` 的模块。PyTorch 提供 `torch.utils.cpp_extension.BuildExtension` 帮你把编译参数配好；昇腾的 `torch_npu` 在此基础上提供 `NpuExtension`，自动追加上升腾 CANN 的头文件与库路径。
- **`torch.ops` 命名空间**：PyTorch 内置的「算子仓库」。C++ 侧用 `TORCH_LIBRARY` / `TORCH_LIBRARY_FRAGMENT` 宏注册算子签名后，Python 侧就能通过 `torch.ops.<命名空间>.<算子名>` 拿到一个可直接调用的对象。本仓库用的命名空间叫 `custom`。
- **调度键（dispatch key）**：同一个算子签名可以挂多份实现，按「调度键」区分。本讲只需记住两个：`PrivateUse1`（预留给第三方后端的设备键，`torch_npu` 用它表示 NPU 设备上的真实计算）和 `Meta`（只推形状不计算，供 `torch.compile` 等框架机制使用）。第 3 单元 u3-l1 会展开。
- **eager 模式与图模式**：eager 即逐条立即执行算子；图模式指用 `torch.compile` 把整段 Python 前向编译成计算图再执行。昇腾图模式由 `torchair` 提供后端支持。
- **双包协作回顾（u1-l1/u1-l2）**：run 包（`cust_opapi.so` 等）装进 `opp/vendors/omni_custom_transformer/`，提供 aclnn 接口；wheel 包（`omni_custom_ops`）只做 PyTorch 适配，运行时通过 aclnn 接口「借道」run 包完成真正的 NPU 计算。**顺序必须是先装 run 包并 `source set_env.bash`，再装 wheel 包**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ascendc/torch_ops_extension/setup.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/setup.py) | wheel 打包入口：收集所有 csrc 源码、创建 NpuExtension、配置 setuptools |
| [ascendc/torch_ops_extension/build_and_install.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/build_and_install.sh) | 一键「编译 wheel + pip 安装」脚本 |
| [ascendc/torch_ops_extension/omni_custom_ops/__init__.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/__init__.py) | 包入口：加载 C++ 扩展、导入 converter、把算子挂载到 `torch_npu` |
| [ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp) | 所有算子的 `torch.ops.custom` 签名定义（本讲只看注册机制，逐行精读留给 u3-l1） |
| [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp) | 下三角求逆算子的 csrc 实现，本讲代码实践的主角 |
| [ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/example/test_npu_chunk_gated_delta_rule_recurrence.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/example/test_npu_chunk_gated_delta_rule_recurrence.py) | 仓库自带的调用示例：eager 与图模式两种写法、NPU/CPU 精度对比 |
| [ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/st/test_lower_triangular_inverse.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/st/test_lower_triangular_inverse.py) | 下三角求逆的真机测试，提供构造输入与校验结果的参考做法 |
| [ascendc/README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md) | 官方编译安装说明（L294-L301 是 wheel 包部分） |

## 4. 核心概念与源码讲解

### 4.1 wheel 打包：setup.py + NpuExtension

#### 4.1.1 概念说明

`torch_ops_extension` 目录的任务是把散落在 `omni_custom_ops/` 各处的 C++ 适配代码（csrc）和 Python 代码（converter）打成一个可 pip 安装的 wheel 包。它不编译任何 AscendC kernel——kernel 和 aclnn 接口属于 run 包（u1-l2）；wheel 只负责「让 PyTorch 认识这些算子」。

打包要解决三个问题：

1. **编译哪些源码？** csrc 文件分散在几十个算子目录里，`setup.py` 用两条 glob 规则自动收集，新增算子目录无需改打包脚本。
2. **用什么编译参数？** csrc 代码引用了 `torch_npu` 与昇腾 ACL 头文件，`NpuExtension` 负责把这些路径配好。
3. **产物怎么进 wheel？** 编译出的 `.so` 不是 Python 文件，必须在 `package_data` 里显式声明才会被打进包里。

#### 4.1.2 核心流程

执行 `bash build_and_install.sh` 后的完整流程：

```text
build_and_install.sh
  ├─ rm -rf build                       # 清掉历史编译结果，保证干净构建
  ├─ python3 setup.py build bdist_wheel # 走 setuptools
  │    ├─ 收集源码：csrc_base/*.cpp + omni_custom_ops/*/*/*/csrc/*.cpp
  ├─ NpuExtension(name="omni_custom_ops.custom_ops_lib", sources=...)
  │    ├─ 探测 torch_npu 安装路径，追加 ACL 头文件目录与编译宏
  │    └─ 编译链接 → omni_custom_ops/custom_ops_lib.*.so
  ├─ find_packages() 收集所有含 __init__.py 的 Python 包（converter 等）
  ├─ package_data 声明 *.so 也要打进 wheel
  └─ dist/omni_custom_ops-1.0-<py版本>-<py版本>-<arch>.whl
  └─ pip3 install *.whl --force-reinstall   # 装进当前 Python 环境
```

两条 glob 规则值得记住（对应仓库目录结构）：

- `omni_custom_ops/csrc_base/*.cpp`：公共适配层，包括所有算子的签名定义 `ops_def_registration.cpp` 和通用调用工具 `ops_common.cpp`。
- `omni_custom_ops/*/*/*/csrc/*.cpp`：三层目录再进 csrc，正好匹配 `ops_transformer/attention/xxx/csrc`、`ops_nn/matmul/ai_infra_matmul/csrc` 等算子适配目录。

#### 4.1.3 源码精读

先看导入部分——`NpuExtension` 来自 `torch_npu`，是 PyTorch `CppExtension` 的昇腾版：

- [ascendc/torch_ops_extension/setup.py:L16-L17](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/setup.py#L16-L17)：导入 `torch_npu` 与 `NpuExtension`，这是整个打包能找到昇腾头文件/库的前提（也意味着**打包机器上必须已装 torch_npu**）。
- [ascendc/torch_ops_extension/setup.py:L19-L25](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/setup.py#L19-L25)：用 `torch_npu.__file__` 反推出安装路径，进而定位 ACL 头文件目录 `include/third_party/acl/inc`；`USE_NINJA` 环境变量决定是否用 ninja 加速编译。
- [ascendc/torch_ops_extension/setup.py:L28-L47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/setup.py#L28-L47)：一个小的兼容性探测——扫描 ACL 头文件里有没有 float8 类型定义，有则加 `-DSUPPORT_ACL_FLOAT8` 宏打开相关代码路径。这体现了 wheel 包对「不同版本 CANN/torch_npu 环境」的适配思路。
- [ascendc/torch_ops_extension/setup.py:L49-L50](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/setup.py#L49-L50)：**两条 glob 规则收集全部 csrc 源码**，是「新增算子零改动打包脚本」的关键。
- [ascendc/torch_ops_extension/setup.py:L53-L58](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/setup.py#L53-L58)：创建 `NpuExtension`，扩展模块名 `omni_custom_ops.custom_ops_lib` 决定了产物 `.so` 的位置（`omni_custom_ops/custom_ops_lib.*.so`）和 Python 里的导入名。
- [ascendc/torch_ops_extension/setup.py:L60-L71](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/setup.py#L60-L71)：`setup()` 主调用。注意 `package_data` 把 `*.so` 声明进包数据（否则 `.so` 不会进 wheel），`find_packages()` 收集全部 Python 子包。

再看一键脚本本身：

- [ascendc/torch_ops_extension/build_and_install.sh:L14-L22](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/build_and_install.sh#L14-L22)：三步走——`rm -rf build` 清历史、`python3 setup.py build bdist_wheel` 出包到 `dist/`、`pip3 install *.whl --force-reinstall` 强制重装（`--force-reinstall` 保证反复调试时总是覆盖旧版本）。

官方文档对应说明（产物命名与执行目录）：

- [ascendc/README.md:L294-L301](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/README.md#L294-L301)：进入 `torch_ops_extension` 目录执行 `bash build_and_install.sh`，成功后在 `dist` 目录生成 `omni_custom_ops-1.0-<python_version>-<python_version>-<arch>.whl`。

#### 4.1.4 代码实践

**实践目标**：弄清楚「一条打包命令究竟会编译哪些文件」，有昇腾环境则完整走一遍编译安装。

**操作步骤**：

1. 有环境时（前提：run 包已安装且 `source set_env.bash` 已执行，见 u1-l2）：

   ```bash
   cd inference/ascendc/torch_ops_extension
   bash build_and_install.sh
   ls dist/
   ```

2. 无环境时，用下面的「示例代码」本地复现 setup.py 的源码收集逻辑（纯 glob，不依赖 torch）：

   ```python
   # 示例代码：等价复现 setup.py L49-L50 的源码收集
   import glob, os
   BASE_DIR = "inference/ascendc/torch_ops_extension"  # 按你的实际路径修改
   files = glob.glob(os.path.join(BASE_DIR, "omni_custom_ops/csrc_base", "*.cpp"))
   files += glob.glob(os.path.join(BASE_DIR, "omni_custom_ops/*/*/*/csrc", "*.cpp"))
   for f in sorted(files):
       print(f)
   print("共", len(files), "个 csrc 源文件")
   ```

**需要观察的现象**：

- 有环境：`dist/` 下出现 `omni_custom_ops-1.0-*.whl`；`pip3 list | grep omni` 能看到已安装的包；重跑脚本时 `rm -rf build` 会先清掉旧产物。
- 无环境：输出的文件清单应包含 `csrc_base/ops_def_registration.cpp`、`csrc_base/ops_common.cpp`，以及每个算子目录 csrc 下的 `.cpp`（共约 20 个）。

**预期结果**：能口头回答「新增一个算子目录 `<op>/csrc/xxx.cpp` 后，setup.py 需要改哪里」——答案是**不需要改**，glob 会自动收进来。

（无昇腾环境部分为源码阅读型实践，可本地验证；编译安装部分**待本地验证**。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `package_data` 里必须显式写 `'omni_custom_ops': ['*.py', '*.so']`？漏掉会怎样？

答案：`find_packages()` 只保证把含 `__init__.py` 的目录当作 Python 包收集，但 `.so` 这类非 Python 产物默认不会打进 wheel。漏掉后 wheel 里没有 `custom_ops_lib.*.so`，用户 `import omni_custom_ops` 时 `from . import custom_ops_lib` 会报 `ModuleNotFoundError`，挂载链条从第一步就断掉。

**练习 2**：`build_and_install.sh` 里 `rm -rf build` 删掉的是什么？为什么每次都要删？

答案：删的是 setuptools 的临时编译目录 `build/`（对象文件、中间产物）。C++ 扩展的增量编译有时不能正确感知头文件或编译宏变化，残留旧对象可能导致「改了代码但 wheel 里还是旧逻辑」的诡异问题，干脆每次全量重编。

**练习 3**：在没装 `torch_npu` 的机器上能执行 `python3 setup.py build` 吗？

答案：不能。`setup.py` 在模块顶部就 `import torch_npu`（L16），并用 `os.path.dirname(torch_npu.__file__)` 定位 ACL 头文件（L19、L25）；导入失败会直接抛 `ModuleNotFoundError`。这也说明 wheel 的编译环境必须与目标运行环境的 torch/torch_npu 版本配套。

### 4.2 算子挂载：custom_ops_lib 注册 + `__init__.py` 双通道挂载

#### 4.2.1 概念说明

装好 wheel 只是把文件放进 Python 环境；用户敲 `torch.ops.custom.npu_lower_triangular_inverse` 之前，还差两步「挂载」：

1. **C++ 通道——注册进调度器**。编译产物 `custom_ops_lib.*.so` 里的 `ops_def_registration.cpp` 用 `TORCH_LIBRARY_FRAGMENT(custom, m)` 把所有算子签名（schema）注册到 PyTorch 调度器的 `custom` 命名空间；每个算子自己的 csrc 文件再用 `TORCH_LIBRARY_IMPL` 给 `PrivateUse1`（NPU 真算）和 `Meta`（只推形状）两个调度键挂上实现。`.so` 被 import 的那一刻，这些静态注册代码自动执行。
2. **Python 通道——镜像到 torch_npu**。`__init__.py` 在导入 C++ 扩展之后，遍历 `torch.ops.custom` 里所有公开算子，逐个 `setattr` 到 `torch_npu` 模块上。于是 `torch_npu.npu_lower_triangular_inverse(...)` 与 `torch.ops.custom.npu_lower_triangular_inverse(...)` 完全等价——后者是 PyTorch 的「官方地址」，前者是给习惯 `torch_npu.npu_flash_attention` 写法的用户准备的「快捷方式」。

一句话：**`.so` 负责「注册」，`__init__.py` 负责「转发」**。

#### 4.2.2 核心流程

用户执行 `import omni_custom_ops` 瞬间发生的事：

```text
import omni_custom_ops
  ├─ 先 import torch、import torch_npu          # .so 链接了 torch_npu 符号，顺序不能反
  ├─ from . import custom_ops_lib               # 加载 C++ 扩展
  │    ├─ ops_def_registration.cpp 静态注册：
  │    │    TORCH_LIBRARY_FRAGMENT(custom, m) → m.def("npu_lower_triangular_inverse(Tensor x) -> Tensor") ...
  │    └─ 各算子 csrc 静态注册：
  │         TORCH_LIBRARY_IMPL(custom, PrivateUse1, m) → NPU 真算
  │         TORCH_LIBRARY_IMPL(custom, Meta, m)        → 形状推导
  │      ⇒ 此刻 torch.ops.custom 命名空间已存在
  ├─ import 各算子 converter（torchair 图模式适配，u3-l3 详述）
  └─ 遍历 torch.ops.custom 的公开算子 → setattr(torch_npu, 算子名, 算子对象)
       ⇒ 此刻 torch_npu.npu_xxx 也可用
```

#### 4.2.3 源码精读

- [ascendc/torch_ops_extension/omni_custom_ops/__init__.py:L8-L12](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/__init__.py#L8-L12)：模块 docstring 直接写明了两种调用方式，是理解本模块最好的「自述文件」。
- [ascendc/torch_ops_extension/omni_custom_ops/__init__.py:L16-L20](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/__init__.py#L16-L20)：**先 `import torch`、`import torch_npu`，再 `from . import custom_ops_lib`**。注释解释了原因：必须保证 torch/torch_npu 已成功导入，否则后续挂载操作失败（`.so` 依赖它们的符号）。
- [ascendc/torch_ops_extension/omni_custom_ops/__init__.py:L21-L27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/__init__.py#L21-L27)：导入 6 个算子的 converter 模块。import 的副作用是注册 torchair 的 fx2ge 图转换器，使这些算子能被图模式捕获（细节在 u3-l3，本讲只需知道「这一步服务图模式」）。
- [ascendc/torch_ops_extension/omni_custom_ops/__init__.py:L31-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/__init__.py#L31-L41)：挂载核心——`getattr(torch.ops, 'custom', None)` 拿到命名空间对象，`dir()` 遍历其中所有名字，跳过 `_` 开头的内建属性（如 `__name__`），把每个算子对象 `setattr` 到 `torch_npu` 上。
- [ascendc/torch_ops_extension/omni_custom_ops/__init__.py:L43-L47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/__init__.py#L43-L47)：兜底分支——若 `custom` 命名空间不存在（典型原因：`custom_ops_lib` 没编译进来或没被导入），只发 warning 不报错，此时 `torch.ops.custom.xxx` 与 `torch_npu.xxx` 都不可用，提示信息明确说明两种写法的前提。

C++ 通道的两个注册点（本讲看机制，逐行精读留给 u3-l1）：

- [ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp:L14-L17](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L14-L17)：注释写明「每次新增自定义 aten ir 都需先增加定义」，`TORCH_LIBRARY_FRAGMENT(custom, m)` 允许多个文件共同向 `custom` 命名空间追加签名。
- [ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp:L53](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L53)：本讲实践主角的签名定义——`npu_lower_triangular_inverse(Tensor x) -> Tensor`，一个输入张量、一个输出张量。
- [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp:L20-L26](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp#L20-L26)：NPU 实现——检查输入必须 5 维，`empty_like` 分配输出，然后用 `EXEC_NPU_CMD_V1(aclnnAiInfraLowerTriangularInverse, x, result)` 调用 run 包里的 aclnn 接口（宏的内部机制在 u3-l2 精读）。
- [ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp:L37-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/lower_triangular_inverse/csrc/lower_triangular_inverse.cpp#L37-L43)：两个 `TORCH_LIBRARY_IMPL` 分别把上面的实现挂到 `PrivateUse1`（NPU 设备真算）与 `Meta`（形状推导，函数体只 `empty_like` 不计算）调度键上。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「import 之后两条调用通道都通了」，并理解挂载失败的报错形态。

**操作步骤**：

1. 有环境时（wheel 已安装、NPU 可用），依次执行：

   ```python
   # 示例代码：验证双通道挂载
   import torch
   import torch_npu
   import omni_custom_ops   # 这一行是挂载的开关，删掉后下面两行都会失败

   names = [n for n in dir(torch.ops.custom) if not n.startswith('_')]
   print("custom 命名空间算子数:", len(names))
   print("npu_lower_triangular_inverse 已注册:", hasattr(torch.ops.custom, "npu_lower_triangular_inverse"))
   print("torch_npu 侧已挂载:", hasattr(torch_npu, "npu_lower_triangular_inverse"))
   ```

2. 再做一组对照实验：把 `import omni_custom_ops` 注释掉重跑，观察差异。

**需要观察的现象**：

- 有 `import omni_custom_ops`：`custom 命名空间算子数` 是一个正数（与 `ops_def_registration.cpp` 中 `m.def` 数量一致），两个 `hasattr` 均为 `True`。
- 去掉 import：`torch.ops.custom` 要么不存在、要么没有这些算子；若走到了 `__init__.py` 的兜底分支会打印「torch.ops.custom module is not found...」的 warning。

**预期结果**：确认「不 import omni_custom_ops 就没有这些算子」——挂载不是 pip 安装自动完成的，而是 import 副作用完成的。这也是 example/ST 脚本里都必须先 `import omni_custom_ops` 的原因。

（无昇腾环境时本实践无法运行，**待本地验证**；可先完成 4.2.5 练习 1 的源码推演。）

#### 4.2.5 小练习与答案

**练习 1**：如果把 `__init__.py` 第 20 行 `from . import custom_ops_lib` 移到 `import torch` 之前，会发生什么？

答案：会出问题。`custom_ops_lib.so` 是链接了 libtorch/libtorch_npu 的 C++ 扩展，导入它之前宿主库必须已被加载；`__init__.py` L16 的注释明确写了「Ensure that the torch and torch_npu has been successfully imported to avoid subsequent mount operation failures」。顺序颠倒轻则符号解析失败，重则进程崩溃；即便勉强加载成功，后面的 `getattr(torch.ops, 'custom', None)` 也可能拿不到命名空间，只剩 warning 分支。

**练习 2**：挂载循环里 `if op_name.startswith('_'): continue` 过滤的是什么？不过滤会有什么后果？

答案：`dir(torch.ops.custom)` 会把 `__name__`、`__doc__` 这类内建属性也列出来。不过滤的话，这些非算子对象会被原样 `setattr` 到 `torch_npu` 上，污染 `torch_npu` 模块命名空间，甚至可能覆盖 torch_npu 已有的同名属性。

**练习 3**：用户反馈「pip 里明明装了 omni_custom_ops，但 `torch_npu.npu_lower_triangular_inverse` 报 AttributeError」。请给出两条最可能的排查方向。

答案：① 脚本里忘了 `import omni_custom_ops`——pip 安装只落盘文件，挂载靠 import 副作应；② wheel 里缺 `custom_ops_lib.*.so`（例如 `package_data` 漏配或编译失败被忽略），导致 `from . import custom_ops_lib` 失败、`custom` 命名空间未注册，走到 `__init__.py` L43-L47 的 warning 分支。可分别用 `import omni_custom_ops; omni_custom_ops.custom_ops_lib` 与 `pip show -f omni_custom_ops | grep so` 验证。

### 4.3 Python 调用：eager 与图模式两种用法

#### 4.3.1 概念说明

挂载完成后，调用算子有两种写法、两种模式，共四个常见组合：

| | 写法 A：`torch.ops.custom.npu_xxx(...)` | 写法 B：`torch_npu.npu_xxx(...)` |
| --- | --- | --- |
| eager 模式（逐条执行） | example 脚本用法 | ST 测试用法 |
| 图模式（torch.compile + torchair） | example 脚本用法（需 converter） | 不常用于自定义算子 |

调用时有三条通用规则：

1. **输入张量必须在 NPU 上**（`.npu()` 搬运），因为实现在 `PrivateUse1` 调度键上；CPU 张量会因找不到实现而报错。
2. **多输出算子返回 tuple**，按签名 `-> (Tensor, Tensor)` 顺序解包。
3. **图模式依赖 converter**：`__init__.py` 导入的 6 个 converter 决定了哪些算子能被 torchair 图捕获；不在列表里的算子只能 eager 调用。

此外要建立「算子行为以代码为准」的意识：例如 `npu_chunk_gated_delta_rule_recurrence` 签名只声明两个输出，但它会**原地刷新输入 `initial_state`**（状态类算子的常见设计），这一点从 example 的对拍逻辑里能直接读出来。

#### 4.3.2 核心流程

以 example 脚本的一次 eager 调用为例：

```text
构造 CPU 随机张量 → .npu() 搬到设备
  → torch.ops.custom.npu_chunk_gated_delta_rule_recurrence(7 个 NPU 张量)
      → 调度器按 PrivateUse1 键找到 csrc 的 C++ 实现
      → EXEC_NPU_CMD_V1 → aclnn 接口（run 包）→ tiling → kernel   # u2/u3 详述
  → 返回 (attn_inter_out, v_new_out)；initial_state 原地更新
  → .cpu() 搬回主机，与 CPU 参考实现逐元素对拍
```

#### 4.3.3 源码精读

仓库自带的 example 是最好的调用范本：

- [example/test_npu_chunk_gated_delta_rule_recurrence.py:L9-L17](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/example/test_npu_chunk_gated_delta_rule_recurrence.py#L9-L17)：导入清单——`torch`、`torch_npu`、**`omni_custom_ops`（触发挂载，必不可少）**，以及 `torch_npu.testing.testcase` 提供的 TestCase/run_tests 测试基类。
- [example/test_npu_chunk_gated_delta_rule_recurrence.py:L66-L85](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/example/test_npu_chunk_gated_delta_rule_recurrence.py#L66-L85)：通用测试函数——L74-L81 构造 float32 随机张量并逐个 `.npu()`；注意 L75 先 `clone().detach()` 留下 `initial_state_input` 作为 CPU 参考的输入副本（因为 NPU 算子会原地改 `initial_state`）；L83-L85 调用被测函数并解包两个输出。
- [example/test_npu_chunk_gated_delta_rule_recurrence.py:L104-L108](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/example/test_npu_chunk_gated_delta_rule_recurrence.py#L104-L108)：**eager 调用的最小形态**——`test_eager` 直接 `return torch.ops.custom.npu_chunk_gated_delta_rule_recurrence(*args)`，一行就是一次算子调用。
- [example/test_npu_chunk_gated_delta_rule_recurrence.py:L116-L135](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/example/test_npu_chunk_gated_delta_rule_recurrence.py#L116-L135)：**图模式调用范本**——用 `torchair.get_npu_backend(compiler_config=...)` 拿编译后端，把算子调用包进 `nn.Module.forward`，再 `torch.compile(..., backend=npu_backend, fullgraph=True)` 整图编译执行。`keep_inference_input_mutations = True` 与该算子原地修改输入的行为直接相关。之所以能整图捕获，靠的是 4.2 中导入的 converter。
- [example/test_npu_chunk_gated_delta_rule_recurrence.py:L87-L99](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/example/test_npu_chunk_gated_delta_rule_recurrence.py#L87-L99)：NPU 结果与 CPU 参考实现（L23-L55）对拍，`compare` 用 `np.isclose(rtol=0.005, atol=0.0001)` 且要求 99% 元素达标——这是精度验收的常见写法。

另一种写法（`torch_npu.xxx`）的实例在 ST 测试里：

- [tests/st/test_lower_triangular_inverse.py:L14](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/st/test_lower_triangular_inverse.py#L14)：同样必须先 `import omni_custom_ops`。
- [tests/st/test_lower_triangular_inverse.py:L16-L31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/st/test_lower_triangular_inverse.py#L16-L31)：构造「单位下三角」输入的技巧——先生成严格下三角随机阵（L18-L20），再用 `I - tril(x, k=-1)` 得到对角线为 1 的下三角阵（保证可逆，L22）；L24 一行完成调用与回搬：`torch_npu.npu_lower_triangular_inverse(x.npu()).to("cpu")`。
- [tests/st/test_lower_triangular_inverse.py:L25-L31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/st/test_lower_triangular_inverse.py#L25-L31)：校验思路不是直接算逆矩阵比对，而是验证**逆的定义**——L28 的 `matmul(x, output)` 加 L31 的 `allclose(atol=0.002)`，比构造参考逆实现更稳健，值得借鉴。
- [tests/st/test_lower_triangular_inverse.py:L33-L38](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/st/test_lower_triangular_inverse.py#L33-L38)：真机测试用 `@pytest.mark.resources(device="npu:910B", ...)` 声明所需资源（u6-l2 详述），测试规模为 5 维输入 `(1, 8, 64, 128, 128)`——与 csrc 里 `X_DIM_NUM = 5` 的检查对应。

#### 4.3.4 代码实践

**实践目标**：仿照 example 与 ST 测试，写出调用 `torch.ops.custom.npu_lower_triangular_inverse` 的最小可运行脚本，并用「矩阵乘以逆 ≈ 单位阵」自检。

**操作步骤**：

1. 确认环境就绪：run 包已装、`set_env.bash` 已 source（u1-l2）、wheel 已安装（4.1.4）。
2. 新建 `test_my_lti.py`（示例代码，仿照 [test_lower_triangular_inverse.py:L16-L31](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/st/test_lower_triangular_inverse.py#L16-L31)）：

   ```python
   # 示例代码：最小调用脚本
   import numpy as np
   import torch
   import torch_npu
   import omni_custom_ops   # 关键：触发算子挂载

   a, b, c, m = 1, 2, 4, 64          # 5 维输入 (a, b, c, m, m)
   x = np.random.uniform(-0.4, 0.4, (a, b, c, m, m)).astype(np.float32)
   x = np.eye(m) - np.tril(x, k=-1)  # 对角线为 1 的下三角阵，必可逆
   x = torch.from_numpy(x)

   inv = torch.ops.custom.npu_lower_triangular_inverse(x.npu()).cpu()  # 写法 A
   # inv = torch_npu.npu_lower_triangular_inverse(x.npu()).cpu()       # 写法 B，等价

   prod = torch.matmul(x, inv)
   eye = torch.eye(m).expand(a, b, c, m, m)
   print("max |x@inv - I| =", (prod - eye).abs().max().item())
   print("allclose:", torch.allclose(prod, eye, atol=2e-3))
   ```

3. 运行 `python3 test_my_lti.py`。

**需要观察的现象**：

- 换成写法 B 结果一致；删掉 `import omni_custom_ops` 则报算子不存在。
- 把输入改成 4 维（如去掉 `a` 维）会触发 csrc 的 `TORCH_CHECK` 报错：`value dim should be 5, but actual is 4`。
- `max |x@inv - I|` 是 1e-3 量级的小数，`allclose` 打印 `True`。

**预期结果**：脚本输出 `allclose: True`，证明「wheel 挂载 → torch 调用 → run 包计算」全链路打通。

（本实践需要真实昇腾硬件，**待本地验证**；无环境时请完成第 5 节综合实践中的「无环境替代任务」。）

#### 4.3.5 小练习与答案

**练习 1**：example 里 `test_chunk_gated_delta_rule_recurrence_eager01` 只解包了两个输出，CPU 参考函数却返回三个值。第三个结果从哪里拿到？

答案：从**输入张量 `initial_state` 本身**拿到。NPU 算子调用后 `initial_state` 已被原地刷新为递推后的状态；example 先在 L75 用 `clone().detach()` 保存调用前的副本给 CPU 参考用，调用后再把 `initial_state` 搬回 CPU（L92）与 CPU 参考的第三个输出对拍（L94-L95）。这是状态类推理算子「输入即输出」的典型设计。

**练习 2**：`torch.ops.custom.npu_chunk_gated_delta_rule_recurrence` 返回 tuple 的顺序由什么决定？

答案：由 [ops_def_registration.cpp:L51-L52](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/csrc_base/ops_def_registration.cpp#L51-L52) 中签名的返回类型 `->(Tensor, Tensor)` 决定，Python 侧按声明顺序解包；csrc C++ 实现必须按相同顺序返回。所以「调用前先到 ops_def_registration.cpp 查签名」是本仓库使用算子的固定动作。

**练习 3**：为什么 ST 测试用 `x @ inv ≈ I` 验证，而不是自己实现一个下三角求逆再逐元素对比？

答案：自己实现参考逆不仅工作量大，还可能引入与实现方式相关的数值差异；而「乘以逆等于单位阵」是逆矩阵的数学定义，只依赖 matmul 这一稳定操作，对拍更简单也更可信。这是数值算子测试的常用技巧（间接不变量校验）。

## 5. 综合实践

**任务：从零跑通「装方向盘 → 打火 → 开一圈」的完整链路，并沉淀一份《新算子调用代码清单》。**

有环境（昇腾 NPU + CANN + 已装 run 包）按顺序完成：

1. **装方向盘**：`cd inference/ascendc/torch_ops_extension && bash build_and_install.sh`，确认 `dist/` 生成 `omni_custom_ops-1.0-*.whl` 且 pip 已安装（对应 4.1）。
2. **验证挂载**：运行 4.2.4 的探针脚本，确认 `torch.ops.custom` 与 `torch_npu` 双通道都有 `npu_lower_triangular_inverse`（对应 4.2）。
3. **开一圈**：运行 4.3.4 的最小脚本，记录 `max |x@inv - I|`；再做两组实验——删 `import omni_custom_ops`、把输入降到 4 维，分别记录报错信息，理解每层防线的位置（对应 4.3）。
4. **进阶（可选）**：仿照 example 的 graph 分支，把 lower_triangular_inverse 包进 `nn.Module` 用 `torch.compile(..., backend=torchair.get_npu_backend(...))` 跑一次，体会 eager 与图模式的差别。

无环境替代任务（源码阅读型）：通读 example 与 ST 脚本后，写出《新算子调用代码清单》，至少包含以下 7 项并各给一行依据（文件:行号）：

1. 导入清单：`torch` / `torch_npu` / `omni_custom_ops`（缺一不可的原因）；
2. 调用形态：`torch.ops.custom.<op>(...)` 或 `torch_npu.<op>(...)`；
3. 签名查询处：`ops_def_registration.cpp` 中的 `m.def` 行（参数、默认值、返回 tuple 顺序）；
4. 输入约束：维度/dtype 检查在对应 csrc 的 `TORCH_CHECK`；
5. 设备搬运：输入 `.npu()`、结果 `.cpu()`；
6. 输出处理：tuple 解包；留意签名是否暗示原地更新（对照 example 的 clone 技巧）；
7. 校验方法：构造已知性质输入 + 数值容差对拍（`allclose`/`isclose`）。

## 6. 本讲小结

- wheel 打包由 `setup.py` + `build_and_install.sh` 完成：两条 glob 规则自动收集 `csrc_base` 与各算子 `csrc` 的源码，`NpuExtension` 配好昇腾编译参数，`package_data` 保证 `.so` 进包；产物是 `dist/omni_custom_ops-1.0-*.whl`。
- C++ 扩展 `custom_ops_lib` 在被 import 时通过 `TORCH_LIBRARY_FRAGMENT(custom, m)` 把所有算子签名注册进 PyTorch 调度器，各算子 csrc 再用 `TORCH_LIBRARY_IMPL` 挂上 `PrivateUse1`（NPU 真算）与 `Meta`（形状推导）实现。
- `__init__.py` 的挂载循环把这些算子镜像 `setattr` 到 `torch_npu`，因此 `torch.ops.custom.npu_xxx(...)` 与 `torch_npu.npu_xxx(...)` 完全等价；前提是脚本里必须 `import omni_custom_ops`。
- csrc 实现最终通过 `EXEC_NPU_CMD_V1(aclnnXxx, ...)` 调用 run 包中的 aclnn 接口——wheel 是「方向盘」，run 包是「发动机」，先装 run 包再装 wheel。
- 调用范本看两个文件：example 展示 eager 与 torchair 图模式两种写法及 NPU/CPU 对拍；ST 测试展示 `torch_npu.xxx` 写法与 `x @ inv ≈ I` 的不变量校验技巧。
- 实践中的三条防线层层报错：没 import 包 → 挂载 warning/属性不存在；维度不符 → csrc 的 `TORCH_CHECK`；精度问题 → 对拍断言失败。

## 7. 下一步学习建议

第 1 单元到此完成，你已经能编译安装整套工程并从 PyTorch 调用算子。第 2 单元「AscendC 算子三层结构源码精读」将打开发动机盖：

- **u2-l1（op_host 之 OpDef）**：看算子原型在 CANN 侧如何注册，与本文的 `m.def` 签名定义对照，理解「CANN 侧 OpDef」与「torch 侧 schema」两份声明的关系。
- **u2-l2（op_api 之 aclnn 两段式）**：精读 `EXEC_NPU_CMD_V1` 背后真正调用的 `aclnnXxx` 接口，补全本讲留下的最大悬念。
- 想先深入 PyTorch 侧注册机制（`TORCH_LIBRARY_IMPL`、调度键、`ops_common.h` 的 dlopen 流程）的读者，也可以直接跳到第 3 单元 u3-l1/u3-l2，再回头学第 2 单元。
