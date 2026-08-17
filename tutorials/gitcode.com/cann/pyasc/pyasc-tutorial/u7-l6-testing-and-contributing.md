# 测试体系与贡献流程：为 pyasc 添加一个新接口

## 1. 本讲目标

前六个单元里，我们一直是 pyasc 的「读者」：读懂了 JIT 主链路、FunctionVisitor、Dialect 定义、Pass 与代码发射。本讲切换身份，成为「贡献者」。学完本讲，你应该能够：

1. 说清 pyasc 的测试分层：`python/test/unit`（mock 掉下发的快速单元测试）、`python/test/kernels`（真执行的 kernel 级测试）、`python/test/generalization`（参数化 + 与 torch 对拍的泛化测试），以及 `test/` 下 lit 驱动的后端测试，各自验证什么、不验证什么。
2. 会用 `test/build_llt.sh` 跑 Python UT、跑 lit 测试、生成覆盖率，并理解它如何根据 PR 改动文件做「精准触发」。
3. 按照 `docs/developer_guide.md` 为一个 Ascend C 接口规划完整改动清单：language → td → TableGen/pybind → 发射 → 测试五层各改哪里、哪些层可以跳过。
4. 按照 `CONTRIBUTING.md` 走对贡献流程（L1/L2/L3 分类、Issue、PR、CI 门禁），并在提交前用 pre-commit、ruff、yapf 自检。

## 2. 前置知识

- **mock（打桩）**：单元测试中用假实现替换真实副作用。pyasc 的 UT 用 `unittest.mock.patch` 把「把 kernel 下发到设备」这一步替换成空操作，从而在没有 NPU、甚至没有仿真器执行的情况下验证「编译链路是否走得通」。
- **pytest fixture**：以参数形式注入测试函数的可复用前后置环境。本讲会见到 `mock_launcher_run`、`filecheck`、`backend`、`platform` 四个 fixture。
- **FileCheck**：LLVM 的文本断言工具，按 `CHECK:` / `CHECK-NEXT:` 逐行匹配输出。第 5、6 单元已在 lit 测试中反复使用；本讲会看到它在 **Python UT 内也被复用**。
- **lit（LLVM Integrated Tester）**：执行 `.mlir` 文件中 `RUN:` 行定义的 shell 命令的测试驱动框架，`test/lit.cfg` 只有三行有效配置。
- **测试金字塔直觉**：越往下（unit）越快、越便宜、覆盖越窄；越往上（generalization）越慢、越贵、越接近真实。pyasc 的分层不是随意切的，每一层只承诺一件事。
- **CLA / Issue / PR / sig**：开源社区协作词汇。CLA 是贡献者协议签署；sig（Special Interest Group）是 CANN 社区按领域组织评审的机制，链接见 `CONTRIBUTING.md`。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|---|---|
| [python/test/unit/](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit) | Python 单元测试根目录，按 `codegen / language(adv/basic/core/fwk) / lib / runtime` 分目录，与 `python/asc` 源码目录镜像 |
| [python/test/unit/conftest.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/conftest.py) | UT 公共 fixture：`mock_launcher_run` 桩与 `filecheck` 断言器 |
| [python/test/unit/language/basic/test_vector_binary.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/language/basic/test_vector_binary.py) | 双目向量算子 UT 范本（本讲反复引用） |
| [python/test/kernels/](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/kernels) | kernel 级端到端测试，真执行、校验数值 |
| [python/test/generalization/basic/test_vadd.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/generalization/basic/test_vadd.py) | 泛化测试范本：参数化 dtype × shape × backend，与 torch 对拍 |
| [test/build_llt.sh](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh) | 一键测试入口：跑 Python UT 与 C++ lit，支持覆盖率与 PR 精准触发 |
| [test/lit.cfg](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/lit.cfg) | lit 配置：后缀 `.mlir`，ShTest 格式 |
| [test/Target/AscendC/basic/vec_binary.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir) | lit 后端测试范本（ASC-IR → Ascend C 发射断言） |
| [docs/developer_guide.md](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md) | 「Ascend C Python 编程接口开发指南」：新增接口的五层改动地图 |
| [CONTRIBUTING.md](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/CONTRIBUTING.md) | 贡献指南：特性分类、流程、PR 交付件与合规检查 |
| [.pre-commit-config.yaml](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/.pre-commit-config.yaml) | 提交钩子：clang-format + OAT 开源合规扫描 |
| [pyproject.toml](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/pyproject.toml) | ruff / yapf / coverage 的工具配置 |
| [include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td) | 双目算子 td 定义，本讲实践案例的数据来源 |

## 4. 核心概念与源码讲解

### 4.1 三层 Python 测试与后端 lit：unit / kernels / generalization 的分层定位

#### 4.1.1 概念说明

pyasc 的 Python 侧测试放在 `python/test` 下，分三层；后端另有一套 lit 测试放在仓库根 `test/` 下。四者的分工是理解整个质量体系的钥匙：

| 层 | 位置 | 执行方式 | 验证目标 | 是否需要设备 |
|---|---|---|---|---|
| unit | `python/test/unit` | pytest，**mock 掉 Launcher.run** | 「能编译出合法 Ascend C 即可」：AST→IR→Pass→翻译→毕昇编译整条链不报错 | 无需 NPU，依赖 Model 仿真器的编译路径 |
| kernels | `python/test/kernels` | pytest，**真执行**（Model 或 NPU） | kernel 数值正确（与 numpy 对拍） | Model 模式可无 NPU |
| generalization | `python/test/generalization` | pytest 参数化，真执行（默认 NPU） | 多 dtype × 多 shape × 多 backend 组合下数值正确（与 torch 对拍） | 需要 NPU（Model 被注释掉） |
| lit（后端） | `test/Target/AscendC` 等 | lit + FileCheck | ASC-IR 文本 → Ascend C 文本的发射逐行正确 | 纯文本变换，无需设备 |

「单元测试能生成合法 Ascend C 即可」与「泛化测试校验数值正确性」的分层是本讲规格里点名的学习目标，它的含义是：UT 层不关心 `1.0 + 2.0` 是否等于 `3.0`，只关心 `asc.add` 能走完五步编译并产出可注册的二进制；数值对错交给上层用 `assert_allclose` / `torch.allclose` 兜底。这样 UT 可以毫秒级跑完上千个接口变体，而昂贵的数值验证只覆盖代表性算子。

#### 4.1.2 核心流程

一个典型 UT 的执行流程：

```
setup_function: config.set_platform(Model, check=False)   # 选定仿真平台
    ↓
@asc.jit 装饰 kernel（装饰期抓取源码与 AST，见 u3-l2）
    ↓
kernel[1]() 触发 _run → 查缓存 → codegen → Pass → 毕昇编译
    ↓
Launcher.run 被 mock 拦截（不下发设备、不回拷数据）
    ↓
断言 mock_launcher_run.call_count == 1   ← 即"编译成功且走到了下发口"
```

一个典型 kernels/generalization 测试的执行流程：

```
fixture 注入 backend/platform（命令行或参数化）
    ↓
Host 侧准备 numpy/torch 输入，构造 launch 参数
    ↓
kernel[核数, stream](...) 真实编译 + 下发 + 回拷
    ↓
np.testing.assert_allclose / torch.allclose 与参考实现对拍
```

#### 4.1.3 源码精读

**① UT 的两个公共 fixture。**
[python/test/unit/conftest.py:L35-L38](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/conftest.py#L35-L38) 定义了 `mock_launcher_run`：用 `patch("asc.runtime.launcher.Launcher.run", return_value=None)` 把 u3-l6 精读过的 `Launcher.run` 整个替换为空函数。这就是「不下发设备」的机关——编译链路全真、执行链路全假。

同一个文件的 [L18-L32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/conftest.py#L18-L32) 还有一个 `FileCheck` 类：它取被测 kernel 的**源码文本**做 CHECK 模板，以 `always_compile=True` 强制重编（绕过两级缓存，见 u3-l8），再取 `global_builder.get_ir_module()` 的 MLIR 文本交给 FileCheck 匹配。也就是说，Python UT 里可以直接用「写在你函数源码里的注释」断言生成的 IR——把 u5 系列学过的 mlir 文本检查下沉到了 pytest 里。[L41-L50](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/conftest.py#L41-L50) 是对应的 fixture 与 `--skip-filecheck` 开关（FileCheck 可执行文件不在 PATH 时可用它跳过）。

**② UT 范本：test_add_kernel。**
[python/test/unit/language/basic/test_vector_binary.py:L13-L14](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/language/basic/test_vector_binary.py#L13-L14) 的 `setup_function` 在每个测试前把平台切到 Model 仿真器；[L17-L32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/language/basic/test_vector_binary.py#L17-L32) 的 `test_add_kernel` 就是 developer_guide 里引用的官方示例：一个 kernel 内连测 `add` 的三种重载（count 形态、mask 连续、mask 数组），最后 `add_kernel[1]()` + `assert mock_launcher_run.call_count == 1`。注意它没有 kernel 入参——连参数 ABI 都不必涉及，专注验证「三个重载都能建出正确的 Op」。

**③ kernels 层：真执行 + numpy 对拍。**
[python/test/kernels/conftest.py:L14-L26](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/kernels/conftest.py#L14-L26) 给 pytest 加了 `--backend`（默认 `Model`）和 `--platform`（默认 `Ascend910B1`）两个命令行选项并导出为 fixture——CI 可用 `pytest --backend=NPU` 切换真机。[python/test/kernels/test_vadd.py:L70-L91](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/kernels/test_vadd.py#L70-L91) 是标准姿势：`vadd_launch` 里按 16 核切分 launch 参数（u1-l4 讲过的两层切分），`vadd_kernel[USE_CORE_NUM, rt.current_stream()](...)` 真实下发，最后 `np.testing.assert_allclose(z, x + y)` 用 numpy 做参考实现。整个 kernel 体就是 02_add_framework 的框架风格（TPipe/TQue，见 u2-l6）。

**④ generalization 层：参数化 + torch 对拍。**
[python/test/generalization/basic/test_vadd.py:L98-L105](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/generalization/basic/test_vadd.py#L98-L105) 展示了泛化测试的两个特征：`BACKENDS` 列表中 `Model` 被注释、默认只跑 `NPU`（上板才是泛化的意义）；两组 `parametrize` 把 `dtype × shape`（含 `(153, 834)` 这种非 2 的幂、含余数的形状）笛卡尔积展开为多个用例。[L116](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/generalization/basic/test_vadd.py#L116) 的 `assert torch.allclose(z, x + y)` 用 torch 做参考实现。文件开头还有 `try: import torch except ModuleNotFoundError: pytest.skip(allow_module_level=True)` 的优雅降级——torch 缺失时整个模块跳过而非报错。

**⑤ 后端 lit：三行配置的测试框架。**
[test/lit.cfg:L1-L6](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/lit.cfg#L1-L6) 只声明了名字、`.mlir` 后缀和 ShTest 格式——所有测试逻辑都在 `.mlir` 文件的 `RUN:` 行里。[test/Target/AscendC/basic/vec_binary.mlir:L11](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L11) 的 `RUN: ascir-translate -mlir-to-ascendc %s | FileCheck %s` 就是 u7-l5 讲过的调试工作流的自动化版本：手写 ASC-IR 输入，断言发射出的 C++ 文本。[test/CMakeLists.txt:L9-L30](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/CMakeLists.txt#L9-L30) 把这些文件组装成 `check-ascir` 目标，由 build_llt.sh 或 `make check-ascir` 驱动。

#### 4.1.4 代码实践

1. **实践目标**：跑通一层 UT，并亲眼看懂「mock 下发」的效果。
2. **操作步骤**（前置：已按 u1-l2 安装 pyasc，`pip3 list | grep pyasc` 可见；`FileCheck` 在 PATH 中，若没有，先加 `--skip-filecheck`）：
   ```bash
   cd <仓库根>
   pytest ./python/test/unit/language/basic/test_vector_binary.py -v
   # 只跑单个用例：
   pytest ./python/test/unit/language/basic/test_vector_binary.py::test_add_kernel -v
   # 无 FileCheck 环境的降级跑法：
   pytest ./python/test/unit/language/basic -v --skip-filecheck
   ```
3. **需要观察的现象**：每个测试在数百毫秒到数秒内完成（首次要真编译，二次命中 u3-l8 的文件缓存后明显变快）；测试通过不代表算过了 `1+2=3`，只代表编译链走通。
4. **预期结果**：`test_vector_binary.py` 全部用例 PASSED。本讲义在 CI 只读沙箱中编写，未实际执行，**待本地验证**。
5. 若想把「mock」变成可感知的东西：在 `asc.runtime.launcher.Launcher.run` 的真实实现处临时加一行 `print("real run")`（记得改完还原），再跑 UT——你不会看到任何输出，因为真实 `run` 根本没被调用。

#### 4.1.5 小练习与答案

**练习 1**：为什么 UT 断言的是 `mock_launcher_run.call_count == 1` 而不是输出数值？
**答案**：`Launcher.run` 被 conftest 的 `patch` 替换成空函数，根本没有设备执行、没有数据回拷，无从校验数值。`call_count == 1` 证明「编译成功且流程推进到了下发口」，这正是 UT 层「能生成合法 Ascend C 即可」的定位；数值校验是 kernels/generalization 层的职责。

**练习 2**：`python/test/generalization/basic/test_vadd.py` 中 `BACKENDS` 为什么把 `Model` 注释掉只留 `NPU`？
**答案**：泛化测试的目的是在真实硬件上用多 dtype、多 shape（包括非对齐形状如 `(153, 834)`）验证数值正确性与泛化能力，Model 仿真器价值有限且拖慢 CI；注释而非删除则保留了本地无 NPU 时手动打开跑通调试的途径。

**练习 3**：`python/test/unit` 下有 `codegen / language / lib / runtime` 四个目录，这个布局有什么好处？
**答案**：与 `python/asc` 源码目录一一镜像，改哪层源码就能立刻定位对应测试目录；build_llt.sh 的精准触发（4.2 节）也正是利用了这套命名对应关系，改 `python/asc/language/basic/**` 就只跑 `python/test/unit/language/basic`。

### 4.2 build_llt.sh：一键测试入口与 PR 精准触发

#### 4.2.1 概念说明

[test/build_llt.sh](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh) 是测试体系的总入口（LLT = Low Level Test）。它解决两个问题：

1. **本地一键跑**：`--run_python_ut` 跑 Python UT，`--check-ascir` 跑后端 lit，加 `--cov` 生成覆盖率。
2. **CI 精准触发**：接收 PR 改动文件列表（`-f/--filelist`），分析「改了什么」来决定「跑多少」——改文档不跑测试，改单个模块只跑该模块，改公共路径全量跑。这直接决定了你的 PR 在 CI 上要等多久。

#### 4.2.2 核心流程

```
build_llt.sh [--run_python_ut | --check-ascir] [--llvm_install_path P] \
            [--lit_install_path P] [--cov] [--asan] [--clang] [-f filelist]
    ↓
analyze_pr_filelist()          # 决策：跑不跑？跑多少？
    ├─ 白名单命中（docs/ *.md README ...）→ 跳过该文件
    ├─ 命中 FULL_TEST_PATHS（lib/Dialect、lib/TableGen、include、bin、CMake...）→ 全量双测
    ├─ lib/Target/AscendC/{模块}/ 命中单模块 → C++ 精准测试
    ├─ python/asc/{模块} 命中单模块 → Python 精准测试；多模块 → 全量
    └─ 未知源码路径 → 兜底全量（安全策略）
    ↓
TEST=lit      → run_check_ascir()：clean → cmake → make check-ascir -j
TEST=python_ut→ run_python_ut()：pytest -v ${UT_PATH} -n auto（host 用例串行）
TEST=all      → 两者都跑
    ↓
Exit Code：0 正常完成 / 1 失败 / 200 「无相关改动，跳过」（非错误）
```

#### 4.2.3 源码精读

**① 关键路径变量与模块清单。**
[test/build_llt.sh:L12-L15](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L12-L15) 确定四个根：`BUILD_DIR=test/build`、`OUTPUT_DIR=test/output`、`UT_PATH=python/test/unit`（脚本位于 `test/` 下，用 `../` 回到仓库根再进 `python/test`）。[L70](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L70) 与 [L75](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L75) 是两份「已知模块清单」：C++ 侧 `Adv/Basic/Core/External/Fwk` 对应 `lib/Target/AscendC/` 的子目录，Python 侧八个模块对应 `python/asc/` 的子目录——这正是 u1-l3 讲过的目录镜像在 CI 上的变现。

**② 触发策略三张表。**
[L50-L65](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L50-L65) 的 `NO_TEST_WHITELIST` 列出十五类纯文档/配置改动（`docs/`、`*.md`、`.github/`、`CONTRIBUTING` 等），命中则不触发任何测试——所以「只改讲义和文档」的 PR 不会浪费 CI 资源。[L81-L90](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L81-L90) 的 `FULL_TEST_PATHS` 反向列出「牵一发动全身」的路径：`lib/Dialect/Asc/IR/`、`lib/TableGen/`、`include/`、`bin/`、`CMakeLists.txt`、`cmake/`——改这些（Dialect 定义、TableGen 生成器、公共头）会触发 C++ 与 Python **双全量**，因为它们影响所有 Op 的定义与生成代码。

**③ 决策函数 analyze_pr_filelist。**
[L131-L269](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L131-L269) 是整个策略的核心，注释里写明了五步：白名单 → 全量触发 → 精准匹配 → 公共文件检查 → 未知源码兜底。值得注意的是两处保守设计：`lib/Target/AscendC/` 下**非子目录**的公共 cpp 改动直接全量（[L186-L198](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L186-L198)）；识别不了的源码路径也全量（[L233-L240](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L233-L240)）。宁可多跑，不可漏跑。

**④ run_python_ut：并行与 host 例外。**
[L302-L340](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L302-L340) 中，全量模式跑两条命令：`pytest -v ${UT_PATH} -n auto -k "not host"` 与 `pytest -v ${UT_PATH} -n 1 -k "host"`（[L333-L338](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L333-L338)）——`lib/host` 相关用例涉及在线编译与文件缓存（u7-l3），并发会互相踩，强制串行；其余用例用 pytest-xdist `-n auto` 按核数并行。覆盖率模式（[L320-L331](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L320-L331)）用 `pkg_resources.resource_filename('asc','')` 定位**已安装**的 asc 包作为 `--source`，跑完 `coverage report` + `coverage html` 输出到 `test/cov_py/`。

**⑤ run_check_ascir 与后端覆盖率。**
[L353-L397](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L353-L397)：全量模式 `cmake` 配置（注入 `-DASCIR_LLT_TEST=ON` 等，[L93](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L93)）后 `make check-ascir -j`；精准模式只 `make ascir-opt ascir-translate` 两个工具，再用 `lit -v {模块}.mlir或{模块}/` 直跑该模块的 lit 文件（[L373-L386](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L373-L386)）——复用了 u7-l5 讲过的「ascir-opt 单跑 Pass」思路。`--cov` 时先设 `LLVM_PROFILE_FILE=%p.profraw`，跑完经 `llvm-profdata merge` / `llvm-cov export`（或 gcc 走 lcov）产出 `coverage.info`，过滤掉 `/usr/include`、LLVM/MLIR 源码后由 `genhtml` 生成 `test/cov_ascir/` 报告（[L428-L648](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L428-L648) 的四个辅助函数）。

**⑥ 入口与退出码。**
[L700-L739](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L700-L739) 解析全部命令行参数（`--llvm_install_path` 必填，[L751-L754](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L751-L754) 校验）；[L663-L697](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/build_llt.sh#L663-L697) 的 `build_test` 依据 `TEST` 分派，并在「无需测试」时 `exit 200`——注释明确 200 表示「跳过（非错误状态）」，CI 据此把「文档 PR 不跑测试」当作成功。

#### 4.2.4 代码实践

1. **实践目标**：用 build_llt.sh 跑一次 Python UT 并生成覆盖率报告。
2. **操作步骤**（前置：`LLVM_INSTALL_PATH` 指向 u1-l2 下载的 LLVM 预编译包；已 `pip install -e .`；装有 `coverage`）：
   ```bash
   cd <仓库根>/test
   bash build_llt.sh --run_python_ut \
        --llvm_install_path ${LLVM_INSTALL_PREFIX} --cov
   # 打开报告：
   ls ../test/cov_py/ && <浏览器> 打开 cov_py/index.html
   ```
   只想跑单模块 UT 又不想等 cmake：直接 `pytest ./python/test/unit/language/basic -v -n auto`（4.1.4 已做过）。
3. **需要观察的现象**：日志先打印 `Test decision: CPP=false(all), Python=true(all)` 一类的决策行；UT 以多进程并行；结束后 `cov_py/` 出现 `.coverage` 与 html。
4. **预期结果**：全量 UT 通过，覆盖率报告能看到 `asc/language`、`asc/codegen` 等目录的行覆盖。本沙箱环境无 LLVM 与已安装的 asc 包，**待本地验证**。
5. 想体验精准触发：`git diff --name-only master > /tmp/fl.txt` 后 `bash build_llt.sh --run_python_ut -f /tmp/fl.txt --llvm_install_path ...`，观察日志中 `precise test target` 是否落在你实际修改的模块上。

#### 4.2.5 小练习与答案

**练习 1**：一个 PR 只改了 `python/asc/language/basic/vec_binary.py` 和 `docs/xxx.md`，CI 会怎么跑？
**答案**：`docs/xxx.md` 命中 `NO_TEST_WHITELIST` 被忽略；`python/asc/language/basic/...` 命中 `KNOWN_PYTHON_MODULES` 中的 `language/basic`，且只命中这一个模块，于是 `PYTHON_TEST_TARGET=language/basic`，只跑 `python/test/unit/language/basic` 的精准 pytest；未涉及 C++ 路径，Lit 测试直接 `exit 200` 跳过。

**练习 2**：为什么把 `include/`、`lib/TableGen/` 放进 `FULL_TEST_PATHS`，而不是也做模块精准匹配？
**答案**：`include/ascir` 是全部 Dialect 与 Op 的定义源头，`lib/TableGen/` 是 pybind/发射代码的生成器（u5-l4）——任何一处改动会同时改变所有模块的生成代码，无法按模块切割影响面，只能全量验证。这是「影响面决定测试面」的原则体现。

**练习 3**：`exit 200` 为什么要单独定义，而不是直接 `exit 0`？
**答案**：语义上区分「测试跑了且通过」（0）与「根本没有需要跑的测试」（200），便于 CI 与人工判断 PR 是否真的被验证过；两者对门禁都算通过，但日志可审计。若未来要求「文档 PR 也至少跑冒烟」，只需改 200 分支而不动成功路径。

### 4.3 developer_guide：新增接口的五层改动地图

#### 4.3.1 概念说明

[docs/developer_guide.md](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md)（《Ascend C Python 编程接口开发指南》）是本讲的地图。它开篇（[L3-L13](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L3-L13)）把新增一个 Ascend C API 的 Python 接口拆成四个开发模块、五类交付件：

- **（必需）Python 前端模块**：新增 Python 接口代码（`python/asc/language/...`）；
- **（必需）ASC-IR 定义模块**：新增 Op 节点定义（`include/ascir/Dialect/Asc/IR/...` 的 td）；
- **（非必需）AST 转 ASC-IR 模块**：只有新语法或 pybind 无法自动绑定时才动（`codegen/function_visitor.py`、`python/src/OpBuilder.cpp`）；
- **（非必需）Ascend C 代码生成模块**：只有不支持自动发射（如数组 mask 入参）时才手写 `printOperation`；
- 交付件必含：前端接口代码、IR 定义代码、**UT 用例（Python 前端 UT + ASC-IR UT）**、API 资料；ST（单算子端到端）非必需。

把「非必需」两项配上 u5-l4 的结论——规整 Op 的 pybind 绑定与发射代码由 TableGen 自动生成——就得到贡献 checklist 的骨架：**多数简单接口只需改三层：language → （td，若后端已有则跳过）→ 测试**。

#### 4.3.2 核心流程

developer_guide 给出的 Python 前端四步（以 Add 为例）：

```
Step1 定归属文件     add 属双目矢量 → python/asc/language/basic/vec_binary.py
Step2 写 overload 存根  三种重载签名（count / mask连续 / mask数组），只有类型提示
Step3 写实现          @require_jit + @set_binary_docstring
                      op_impl("add", dst, src0, src1, args, kwargs,
                              builder.create_asc_AddL0Op,
                              builder.create_asc_AddL1Op,
                              builder.create_asc_AddL2Op)
                      └─ OverloadDispatcher 注册变体 → 命中即建 IR
Step4 加引用          language/basic/__init__.py 与 language/__init__.py
```

与之配套的两条硬规则（[L920-L934](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L920-L934)）：

- 模板参数一律改为运行时参数；常量类型（枚举/bool）直接用，非常量类型用 `asc.ConstExpr[origin_type]` 标记；
- 参数顺序统一重排为：**运行时必选 → 模板必选 → 运行时可选 → 模板可选**（这正是 u2-l5、u5-l3 讲过的四段式）。

#### 4.3.3 源码精读

**① 前端实现三段式的权威出处。**
[docs/developer_guide.md:L93-L105](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L93-L105) 给出 `add` 的实现体：`@require_jit` 守门（u5-l6 讲过的 JIT 编译期判据）、`set_binary_docstring` 自动生成 API 文档、`op_impl` 传入三个 `builder.create_asc_AddL{0,1,2}Op` 构造方法；[L107-L136](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L107-L136) 展开 `op_impl` 内部：`OverloadDispatcher` 三个 `@dispatcher.register` 变体分别对应 L0/L1/L2，标量统一经 `_mat`（`materialize_ir_value`，u2-l3）物化成 IR 句柄。这三段式与你在 u2-l5 读过的 `vec_binary.py` 真实源码完全一致——指南写的就是仓库现状。

**② UT 开发的官方范式。**
[docs/developer_guide.md:L278-L364](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L278-L364) 规定 UT 目录与源码镜像、四步框架（注入 mock 桩 → 定义 kernel → 触发运行 → 断言），并给出执行命令 `pytest ./python/test/unit/language/basic/test_vector_binary.py`（[L360-L364](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L360-L364)）。

**③ ASC-IR 定义层：模板复用。**
[docs/developer_guide.md:L648-L699](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L648-L699) 演示符合模板的 API 一行搞定：`defm Add : BinaryTemplateL0123Op<"add", "Add", "operator+">;` 展开为 L0/L1/L2/L3 四个 Op（u5-l3 讲过 defm 机制）；不符合模板的（如 `TPipe::InitBuffer`）则按 APIOp 规则手写。`genEmitter` 由 `AscConstructor/AscMemberFunc/AscFunc` 三个 trait 自动置位（[L472-L487](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L472-L487)），`paramTypeLists` 的 `-3..5` 整数编码含义在 [L497-L509](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L497-L509) 有官方表格（u5-l4 已精读其实现）。

**④ 发射层何时才需要动手。**
[docs/developer_guide.md:L770-L847](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L770-L847)：带 Asc trait 的 Op 配好 td 即自动发射（`SetAtomicAdd` 例子，`paramTypeLists = [5]`）；不支持的（数组 mask 等）才需三件事——在 `Translation.cpp` 的 `PrintableOpTypes` 注册、在 `include/ascir/Target/Asc/...` 声明 `printOperation`、在 `lib/Target/AscendC/...` 实现（`GatherL1Op` 例子）。lit UT 与 ASC-IR 定义模块共用（[L726-L767](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L726-L767)）。

**⑤ 一个真实的「缺口」案例：FusedAbsSub / FusedExpSub / Prelu。**
这是本讲为综合实践选好的靶子，后端三层全部就绪、唯独缺 Python 前端：

- td 已定义：[include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td:L28-L29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td#L28-L29) `def FusedAbsSubL2Op : BinaryL2Op<"fused_abs_sub_l2", "FusedAbsSub">`、`def FusedExpSubL2Op : ...`，以及 [L37](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td#L37) `def PreluL2Op : BinaryL2Op<"prelu_l2", "Prelu">`（模板类 `BinaryL2Op` 定义于 [include/ascir/Dialect/Asc/IR/Base.td:L143](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L143)）。
- 发射已被 lit 验证：[test/Target/AscendC/basic/vec_binary.mlir:L101-L102](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L101-L102) 与 [L111](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L111) 断言输出 `AscendC::FusedAbsSub(v1, v2, v3, v4)`、`AscendC::Prelu(...)`；输入 IR 是 [L122](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L122) 的 `ascendc.fused_abs_sub_l2 %dst, %src0, %src1, %calCount_i32` 与 [L132](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L132) 的 `ascendc.prelu_l2 ...`——四参数（dst、src0、src1、count），L2 单级。
- 前端确实缺失：[python/asc/language/basic/vec_binary.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py) 的顶层函数清单里有 `add/sub/mul/div/max/min/...` 与 `sub_relu`（[L400 起](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L400)）等 17 个名字，但没有 `fused_abs_sub`、`fused_exp_sub`、`prelu`（已 grep 确认）。

#### 4.3.4 代码实践

1. **实践目标**：为一个尚缺 Python 封装的接口写出五层改动清单（只写清单，不要求实现）。
2. **操作步骤**：靶子选 `Prelu`。按 developer_guide 的四步逐一落点，先做三个侦查动作：
   - `grep -n "prelu" python/asc/language/basic/vec_binary.py python/asc/language/basic/__init__.py` → 确认前端缺口；
   - `grep -n "prelu" include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td test/Target/AscendC/basic/vec_binary.mlir` → 确认后端就绪；
   - 在构建产物中查 pybind 绑定是否已生成（u5-l4 结论：`-gen-pybind-defs` 遍历全部 Op 记录，与是否带 Asc trait 无关）：`grep -rn "create_asc_PreluL2Op" build/*/python/src/ 2>/dev/null` 或在安装包内 `python -c "import asc._C as C; print([m for m in dir(C.ir) if 'Prelu' in m])"`。
3. **改动清单模板**（以 `prelu` 为例，写进你的 PR 描述）：
   | 层 | 文件 | 改动 | 必需性 |
   |---|---|---|---|
   | language | `python/asc/language/basic/vec_binary.py` | 仿 `sub_relu`（L400 起的三段式）加 `@overload def prelu(dst, src0, src1, count:int)` 存根 + `@require_jit` 实现，调用 `builder.create_asc_PreluL2Op`；因只有 L2 一级，**一个重载即可** | 必需 |
   | language 引用 | `python/asc/language/basic/__init__.py` | 导出 `prelu` | 必需 |
   | td | `include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td` | **无需改动**（`PreluL2Op` 已在 L37） | 已就绪 |
   | pybind/发射 | `python/src/OpBuilder.cpp`、`lib/Target/AscendC/...` | 预计**无需改动**（绑定由 TableGen 自动生成、发射已过 lit）——以步骤 2 第三个侦查动作的结果为准，**待本地验证** | 视侦查结果 |
   | Python UT | `python/test/unit/language/basic/test_vector_binary.py` | 仿 `test_add_kernel` 加 `test_prelu_kernel`：kernel 内 `asc.prelu(z_local, x_local, y_local, count=512)`，断言 `mock_launcher_run.call_count == 1` | 必需 |
   | 资料 | API docstring | 用 `set_binary_docstring(cpp_name="Prelu", ...)`（贡献指南将 API 资料列为必需交付件） | 必需 |
4. **需要观察的现象**：清单中「必需」项恰好落在 developer_guide L3-L7 的两个必需模块；「跳过」项都有 grep 证据支撑，而不是凭感觉。
5. **预期结果**：得到一份可执行的 PR 改动清单。真正实现属于本讲综合实践（见第 5 节）。

#### 4.3.5 小练习与答案

**练习 1**：developer_guide 说「AST 转 ASC-IR 模块」和「Ascend C 代码生成模块」是非必需的，什么情况下才必须动它们？
**答案**：前者两种场景：要支持尚不在白名单里的新 Python 语法（需在 `function_visitor.py` 加 `visit_*`，见 u4-l5），或 Op 无法被 `-gen-pybind-defs` 自动绑定（复杂 API Type、继承 `AscendC_BaseTensorType` 的类型、含枚举参数的 Op，需在 `OpBuilder.cpp` 手写 `.def`，见指南 [L392-L459](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L392-L459)）。后者一种场景：接口入参含数组类型等自动发射不支持的形态（如 L1 数组 mask），需注册 `PrintableOpTypes` 并手写 `printOperation`。

**练习 2**：`prelu` 为什么只需一个 `count` 重载，而 `add` 需要三个重载？
**答案**：重载数量由后端 Op 变体决定。`add` 是 `defm Add : BinaryTemplateL0123Op`，展开出 L0（mask 连续）/L1（mask 数组）/L2（count）三个 Op；`Prelu` 只有 `def PreluL2Op : BinaryL2Op`（td L37），lit 中也只有 `AscendC::Prelu(v1, v2, v3, v4)` 一种四参数形态，所以前端一个重载、一个 `create_asc_PreluL2Op` 就够。

**练习 3**：指南要求 Python 函数名用 `lower_with_under`，而 Ascend C 用 `CapWords`。`FusedAbsSub`、`GetPhyAddr` 对应的 pyasc 名字应该是什么？
**答案**：按 [L862-L872](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L862-L872) 的命名表，函数一律小写下划线：`fused_abs_sub`、`get_phy_addr`。仓库里 `bitwise_or` 对应 Ascend C 的 `Or` 也是同一规则的体现（Python 关键字/可读性冲突时用近义词）。

### 4.4 贡献 checklist：从 Issue 到 master 合入 + 提交前自检

#### 4.4.1 概念说明

写完代码只是贡献的一半。`CONTRIBUTING.md` 定义了流程，`.pre-commit-config.yaml` 与 `pyproject.toml` 定义了提交前自检。先看特性分级（[CONTRIBUTING.md:L9-L13](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/CONTRIBUTING.md#L9-L13)）：

| 级别 | 定义 | 例子 |
|---|---|---|
| L1 轻量特性 | < 200 行的新增/修复/优化/文档纠错 | **新增 Ascend C Python API 接口**、Bug 修复、API 文档纠错 |
| L2 大特性 | 大功能/性能增强 | 新增未支持的 Pass 优化大颗粒特性 |
| L3 架构变更 | 核心接口变更/重大重构 | 对外接口目录调整、端到端流程变更 |

关键结论：**4.3 节规划的 `prelu` 这类新接口贡献属于 L1**，走最短路径；L2/L3 要过 sig 评审并先入 experimental 分支。

#### 4.4.2 核心流程

L1 流程（[L21-L46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/CONTRIBUTING.md#L21-L46)）：

```
建 Issue（Requirement|需求建议 或 Bug-Report，评论 /assign 认领）
  → 本地开发验证（本讲 4.1-4.3 + 自检）
  → 提交 PR（experimental 或 master 分支流程）
  → 标记 Issue 完成
```

master 分支的 PR 关键路径（[L112-L127](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/CONTRIBUTING.md#L112-L127)）：

```
Fork → 本地开发验证 → 提 PR → 评论 compile 触发 CI 门禁
  （门禁四项：代码编译、静态检查、UT 测试、冒烟测试）
  → Committer 代码检视 → 闭环意见 → lgtm/approve → 合入
```

#### 4.4.3 源码精读

**① PR 交付件与合规检查。**
[CONTRIBUTING.md:L131-L153](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/CONTRIBUTING.md#L131-L153) 要求：代码交付件 = 功能实现 + 测试用例（新 Ascend C Python 接口按 developer_guide 完成）；文档交付件 = 新特性 README 必选；合规 = C++/Python 编码规范 + 编译通过 + Markdown 语法 + CLA 签署。也就是说，4.3.4 的改动清单里「UT 用例」与「API 资料」不是加分项，是**门禁项**。

**② 提交前自检一：pre-commit 钩子。**
[.pre-commit-config.yaml:L1-L18](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/.pre-commit-config.yaml#L1-L18) 挂了两个钩子：`clang-format`（v16.0.0，作用于 c/c++ 文件，对应 `.clang-format` 的 LLVM 风格配置）与本地 `oat-check`（执行 `scripts/oat_check.sh`，做 OAT 开源合规扫描——检查引入的第三方代码许可）。启用方式：`pre-commit install` 后每次 `git commit` 自动跑；也可 `pre-commit run --all-files` 手动全量。

**③ 提交前自检二：ruff + yapf。**
developer_guide 的编码规范节（[L849-L857](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L849-L857)）给出命令：`ruff check --fix ./python/asc` 与 `yapf -i --parallel -r ./setup.py`（文档中为 Windows 路径写法）。两者的参数在 [pyproject.toml:L10-L24](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/pyproject.toml#L10-L24)：yapf 基于 pep8、行宽 120；ruff 行宽 120、规则集 `E4/E7/E9/F`（pyflake 全家 + 部分 pycodestyle）、忽略 `E731`（允许 lambda 赋值）。改完 `vec_binary.py` 后跑一遍，CI 的「静态检查」门禁就不会拦你。

**④ 门禁与 build_llt.sh 的对应关系。**
CI 门禁四项中的「UT 测试」正是 4.2 节的 `build_llt.sh --run_python_ut` / `--check-ascir`（PR 文件列表经 `-f` 传入，触发精准测试）；「代码编译」对应 u1-l2 的 setup.py 构建；「冒烟测试」对应 `test/run_presmoke_model_test.sh`、`run_presmoke_npu_test.sh`、`run_aftersmoke_test.sh` 等脚本。你在本地把 4.2 的命令跑绿，基本就预演了门禁。

#### 4.4.4 代码实践

1. **实践目标**：对一次（假想的）`prelu` 贡献完成提交前自检。
2. **操作步骤**（在 4.3.4 清单的基础上，不写业务代码也能先做环境自检）：
   ```bash
   pip install pre-commit ruff yapf    # 若未安装
   pre-commit install                   # 挂钩子
   # 全量自检（未改代码时应全部通过，相当于验证工具链本身）：
   pre-commit run --all-files
   ruff check ./python/asc
   yapf --diff --recursive ./python/asc/language/basic | head   # 只看格式差异，-i 才落盘
   ```
3. **需要观察的现象**：clang-format 只检查 c/c++ 文件，纯 Python 改动不会触发它；`ruff check` 无 `F401`（未用导入）一类告警；`yapf --diff` 输出为空表示格式已达标。
4. **预期结果**：三个工具全部通过。若 `oat-check` 因环境缺 OAT 工具报错，记录报错并在 PR 描述说明（CI 侧会补跑），**待本地验证**。
5. 把 4.3.4 的改动清单与本节的流程合成一页 PR 草稿：标题（如 `add prelu python api for basic vector binary ops`）、关联 Issue 号、改动清单表、本地自检与 UT 结果截图。

#### 4.4.5 小练习与答案

**练习 1**：新增一个 Ascend C Python 接口属于 L1 还是 L2？流程差在哪？
**答案**：L1（CONTRIBUTING.md 分级表明确把「新增 Ascend C Python API 接口」列为 L1 示例，且 < 200 行）。L1 只需 建 Issue → 开发 → 提 PR → 检视合入；L2/L3 还需架构师方案预讨论、sig 例会评审、先合 experimental 分支验证充分后再走 master 门禁。

**练习 2**：CI 门禁评论 `compile` 后会跑哪些检查？其中哪一项你在本地已经会用脚本预演？
**答案**：代码编译、静态检查、UT 测试、冒烟测试四项（[L119-L122](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/CONTRIBUTING.md#L119-L122)）。UT 测试可用 `test/build_llt.sh --run_python_ut --llvm_install_path ...`（可加 `-f` 文件列表复现精准触发）本地预演；静态检查用 `ruff check` + `yapf`（以及 C++ 侧 pre-commit 的 clang-format）预演。

**练习 3**：为什么 pre-commit 里 OAT 检查放在 `commit` 阶段且 `pass_filenames: true`？
**答案**：OAT 扫描开源合规（许可证、第三方代码来源），只需针对本次实际改动（staged 的文件）增量检查，放在 commit 阶段能在问题代码进入仓库历史之前拦截；`pass_filenames: true` 让钩子拿到暂存文件列表做精准扫描，避免每次全仓扫描的开销。

## 5. 综合实践

**任务：把 4.3.4 的清单真正落地——为 `prelu` 补齐 Python 前端封装与 UT，并走完自检**（预计 60–120 分钟，需要 u1-l2 的完整构建环境；无 NPU 也可完成，UT 用 Model 仿真 + mock）。

1. **实现**：在 `python/asc/language/basic/vec_binary.py` 中仿照 `sub_relu`（[L400 起](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L400)）写入：
   - 一个 `@overload` 存根：`def prelu(dst: LocalTensor, src0: LocalTensor, src1: LocalTensor, count: int, is_set_mask: bool = True) -> None: ...`（是否保留 `is_set_mask` 请对照 Ascend C `Prelu` 原型确认，**待确认**）；
   - `@require_jit` 实现，内部调 `builder.create_asc_PreluL2Op(dst.to_ir(), src0.to_ir(), src1.to_ir(), _mat(count, KT.int32).to_ir())`——若该方法不存在（说明 pybind 绑定未自动生成），回到 4.3.4 步骤 2 的第三个侦查动作排查。
2. **导出**：在 `python/asc/language/basic/__init__.py` 增加 `prelu` 引用。
3. **测试**：在 `python/test/unit/language/basic/test_vector_binary.py` 加 `test_prelu_kernel`，跑 `pytest ./python/test/unit/language/basic/test_vector_binary.py -v`。
4. **对照验证**：设 `PYASC_DUMP_PATH` 重跑（或直接用 `always_compile=True` 的 filecheck fixture），在导出的 `ascendc.cpp` 中找到 `AscendC::Prelu(...)` 调用，与 lit 断言（[vec_binary.mlir:L111](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/basic/vec_binary.mlir#L111)）互为印证。
5. **自检**：`ruff check --fix ./python/asc`、`yapf -i`、`pre-commit run --all-files`。
6. **收尾**：按 4.4.2 的 L1 流程起草 Issue 与 PR 描述（改动清单表 + UT 结果 + dump 对照），完成一次完整的「读者 → 贡献者」身份切换。

## 6. 本讲小结

- pyasc 的 Python 测试分三层：`unit`（mock 掉 `Launcher.run`，只验证「能编译出合法 Ascend C」，快且无需设备）、`kernels`（真执行 + numpy 对拍）、`generalization`（参数化 dtype×shape×backend + torch 对拍，默认 NPU）；后端另有 `test/` 下 lit + FileCheck 的文本级发射测试，四层各承诺一件事。
- `test/build_llt.sh` 是测试总入口：`--run_python_ut`/`--check-ascir` 二选一或全跑，`--cov` 出 Python（coverage）与 C++（llvm-cov/lcov）两套报告；`-f` 文件列表驱动「白名单跳过 → 核心路径全量 → 单模块精准 → 未知兜底全量」的 CI 触发策略，`exit 200` 表示无需测试。
- developer_guide 把新增接口拆为五层：language 前端（必需，三段式：overload 存根 + `@require_jit` 实现 + `create_asc_*`）、td 定义（必需但常已就绪，模板 `defm` 一行展开多级 Op）、AST/pybind（仅特殊场景手写）、发射（仅数组等特殊入参手写 `printOperation`）、测试与资料（必需交付件）。
- 实战案例 `Prelu/FusedAbsSub/FusedExpSub`：td 定义与 lit 发射断言均已存在（OpVecBinary.td L28-L37、vec_binary.mlir L101-L132），唯独 Python 前端缺封装——是练习「最小改动清单」的理想靶子。
- 贡献流程按 L1/L2/L3 分级，新 API 接口属 L1：Issue → 开发 → PR → `compile` 触发门禁（编译、静态检查、UT、冒烟）→ Committer 检视 → 合入；提交前用 pre-commit（clang-format + OAT）、ruff、yapf 自检，配置集中在 `pyproject.toml` 与 `.pre-commit-config.yaml`。

## 7. 下一步学习建议

- **动手真实贡献**：完成第 5 节综合实践后，把 `FusedAbsSub`、`FusedExpSub` 也补齐，作为第一个 PR 提交；观察 CI 门禁中精准测试是否只跑了 `language/basic`。
- **深入冒烟链路**：阅读 `test/run_presmoke_model_test.sh`、`run_presmoke_npu_test.sh` 与 `test/run_test.py`，理解门禁第四项「冒烟测试」与 UT 的边界。
- **回读源码巩固**：带着贡献者视角重读 [python/asc/language/basic/vec_binary.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py)（对照 u2-l5 的三段式）与 [include/ascir/Dialect/Asc/IR/Base.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td)（对照 u5-l3 模板族），你会发现「读讲义时看懂」与「能照着写」之间恰好隔着本讲这份 checklist。
- **延伸文档**：`docs/API_docstring_generation_tool_guide.md`（API 资料自动生成，贡献必需交付件之一）与 CANN 社区的《PR 操作指南》《Issue 操作指南》（链接见 CONTRIBUTING.md）。
