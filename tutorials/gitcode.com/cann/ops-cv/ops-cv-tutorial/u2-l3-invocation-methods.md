# 算子调用全景：快速调用、业务集成与 PyTorch 扩展

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 ops-cv 官方文档定义的两类调用场景（快速调用 vs 业务集成）与三种调用方式（PyTorch API / aclnn API / GE 图模式）之间的对应关系。
2. 熟练使用 `build.sh --run_example` 一条命令编译并运行仓库中任意算子的样例，包括自定义算子包、ops-cv 整包两种包模式。
3. 看懂一个自建 aclnn 调用工程的 CMakeLists 关键差异（链接 `libcust_opapi.so` 还是 `libopapi_cv.so`）。
4. 理解 `examples/fast_kernel_launch_example` 如何通过 PyTorch Extension 机制把一个 Ascend C Kernel 注册为 `torch.ops.ascend_ops.add` 这样的原生风格 API。

## 2. 前置知识

- **两段式接口**：在 u2-l1 已学过，aclnn 单算子调用分两段——`aclnnXxxGetWorkspaceSize` 负责参数校验并生成 `aclOpExecutor`，`aclnnXxx` 把任务异步下发到 stream。本讲不再重复细节，只把它当作"被调用"的能力。
- **自定义算子包与 vendor 目录**：在 u1-l4 已学过，`build.sh --pkg --ops` 会产出自解压 run 包，安装到 `${ASCEND_HOME_PATH}/opp/vendors/<vendor_name>` 下，其 aclnn 实现库位于 `op_api/lib/libcust_opapi.so`。
- **PyTorch Extension**：PyTorch 允许用 C++ 编写扩展模块，编译成 `.so` 后从 Python `import`。本讲的 fast_kernel_launch_example 用它承载 Ascend C Kernel。
- **PyTorch 算子分发（Dispatch）**：PyTorch 中每个算子有一个 schema（签名），按"调度键"（Meta、CPU、CUDA、PrivateUse1 等）路由到不同后端实现。`PrivateUse1` 是预留给第三方后端的调度键，TorchNPU 用它把算子路由到 NPU。
- **`<<<>>>` 核函数启动语法**：Ascend C 借鉴 CUDA 的 `kernel<<<numBlocks, shared, stream>>>` 语法启动核函数，区别在于第二个参数传 `nullptr`（NPU 没有 CUDA 意义上的 shared memory 配置）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/zh/invocation/quick_op_invocation.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md) | 官方算子调用总纲：两类场景、三种方式、快速调用命令与自建工程模板 |
| [scripts/build_options.sh](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_options.sh) | `--run_example` 参数的解析逻辑（`checkopts_run_example`） |
| [examples/fast_kernel_launch_example/README.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/README.md) | PyTorch 扩展工程的使用与开发指南 |
| [examples/fast_kernel_launch_example/setup.py](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/setup.py) | Python 打包入口：驱动 CMake 构建、生成 abi3 wheel |
| [examples/fast_kernel_launch_example/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/CMakeLists.txt) | 扩展工程顶层 CMake：收集算子源码、产出 `_C.abi3.so` |
| [examples/fast_kernel_launch_example/csrc/extension.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/csrc/extension.cpp) | 极薄的 Python 模块入口，只为触发 `TORCH_LIBRARY` 静态注册 |
| [examples/fast_kernel_launch_example/csrc/add/ascend910b/add.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/csrc/add/ascend910b/add.cpp) | add 算子完整实现：schema 注册、Meta、Kernel、NPU 调用与注册 |
| [examples/fast_kernel_launch_example/ascend_ops/ops.py](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/ascend_ops/ops.py) | Python 侧算子包装函数 |
| [examples/fast_kernel_launch_example/tests/add/test_add.py](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/tests/add/test_add.py) | pytest 精度验证：NPU 结果与 CPU ATen 对比 |

## 4. 核心概念与源码讲解

### 4.1 调用全景：两类场景 × 三种方式

#### 4.1.1 概念说明

官方文档 [quick_op_invocation.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md) 把"怎么用仓库里的算子"组织成一个二维选择问题：

- **场景维度（要不要搭工程）**：
  - **快速调用**：不动手搭调用工程，直接用项目自带脚本 `build.sh` 编译并运行算子样例。适合"我想快速看看这个算子对不对、输出长什么样"。
  - **业务应用集成**：自己创建调用 cpp、CMakeLists、run.sh，把算子嵌进真实业务工程。适合"算子要进我的产品代码"。
- **方式维度（用哪个 API 面）**：
  - **PyTorch API**：把 Kernel 注册进 PyTorch，用 `torch.ops.xxx.yyy` 调用。
  - **aclnn API**：C 语言两段式接口，无需算子 IR 定义（u2-l1/u2-l2 已深入）。
  - **GE 图模式**：基于算子 IR 构图调用（u2-l4 将深入）。

#### 4.1.2 核心流程

一个"选型"决策树可以这样画：

```
我想用算子
├── 只是想验证 / 体验功能？
│   └── 快速调用：bash build.sh --run_example <op> <mode> [pkg_mode] ...
├── 要集成进业务工程？
│   ├── 业务是 C++ 直接调 → 自建 aclnn 调用工程（cpp + CMakeLists + run.sh）
│   ├── 业务是整图下沉   → GE 图模式工程
│   └── 业务是 PyTorch 训推 → PyTorch Extension（fast_kernel_launch_example）
└── 依赖哪种算子包？
    ├── 自定义算子包（opp/vendors/<vendor>）→ 链接 libcust_opapi.so
    ├── ops-cv 整包                        → 链接 libopapi_cv.so
    └── ops-cv 静态库                       → 手写 run.sh 用 g++ 链接三个静态库
```

#### 4.1.3 源码精读

文档开头的两张表定义了这个二维结构。

场景表（[docs/zh/invocation/quick_op_invocation.md:L9-L12](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md#L9-L12)）：区分"快速调用算子"（无需搭建调用工程）与"业务应用集成算子"（需自行搭建调用工程）。

方式表（[docs/zh/invocation/quick_op_invocation.md:L16-L21](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md#L16-L21)）：列出 PyTorch API、aclnn API、GE 图模式三种调用方式及其一句话定位。注意 aclnn 的描述是"无需提供 IR 定义"——这正是它比 GE 图模式上手快的原因。

#### 4.1.4 代码实践

**实践目标**：在做任何编码之前，先做出一次有依据的选型。

**操作步骤**：

1. 打开 [docs/zh/invocation/quick_op_invocation.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md) 和算子清单 `docs/op_list.md`。
2. 假设三个需求，分别写出你选择的场景与方式：
   - 需求 A：验证 `grid_sample` 在当前板子上的输出数值；
   - 需求 B：把 `roi_align` 嵌入一个 C++ 推理服务（非 PyTorch）；
   - 需求 C：在 PyTorch 训练脚本里替换一个手写算子。

**需要观察的现象**：无（纸面选型练习）。

**预期结果**：A → 快速调用 + eager（aclnn）；B → 业务集成 + aclnn API；C → 业务集成 + PyTorch API。若你的答案与此不同，回到 4.1.2 的决策树核对。

#### 4.1.5 小练习与答案

**练习 1**：快速调用模式支持哪几种包形态？
**答案**：三种——自定义算子包（`--run_example op eager cust`）、ops-cv 整包（`--run_example op eager`）、ops-cv 静态库（手写 `run.sh` 用 g++ 链接，见文档"基于 ops-cv 静态库执行算子样例"一节，[L65-L133](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md#L65-L133)）。

**练习 2**：为什么说 aclnn API "无需提供 IR 定义"？
**答案**：aclnn 是 Host 侧直接暴露的 C 语言函数（`aclnnXxxGetWorkspaceSize` + `aclnnXxx`），调用者只需头文件与库即可调用；而 GE 图模式依赖算子的 GE IR（proto）注册才能被图引擎识别成节点。

### 4.2 快速调用算子：`build.sh --run_example` 剖析

#### 4.2.1 概念说明

快速调用的本质：仓库里每个算子都自带 `examples/test_aclnn_xxx.cpp`（部分还有 `test_geir_xxx.cpp`）样例，`build.sh --run_example` 会替你完成"找到样例 → 现场编译 → 链接算子库 → 执行"的全过程。你不需要写任何一行工程代码。

#### 4.2.2 核心流程

```text
bash build.sh --run_example <op> <mode> [pkg_mode] [--vendor_name=xxx] [--soc=xxx] [--experimental]
   │
   ├─ <op>        算子名，小写下划线，如 grid_sample
   ├─ <mode>      eager（aclnn 调用）或 graph（图模式调用）
   ├─ <pkg_mode>  目前仅支持 cust（自定义算子包）；graph 模式下不传
   ├─ --vendor_name  与构建算子包时的 vendor 一致，默认 custom
   ├─ --soc       NPU 型号（需与算子包编译时一致）
   └─ --experimental  执行 experimental/ 贡献目录下的算子

执行效果：编译并运行 <op>/examples/test_aclnn_<op>.cpp（或 test_geir_<op>.cpp），打印样例输出
```

参数解析落在 `checkopts_run_example` 函数中，它设置 `ENABLE_RUN_EXAMPLE=TRUE` 并逐个摘取位置参数（[scripts/build_options.sh:L624-L631](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_options.sh#L624-L631)）；同时 `check_param` 中有一处联动：启用 run_example 时会强制 `ENABLE_CUSTOM=FALSE`，避免与整包构建冲突（[scripts/build_options.sh:L396-L398](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_options.sh#L396-L398)）。帮助信息见 [scripts/build_options.sh:L255-L266](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_options.sh#L255-L266)，其中还展示了 `--simulator` 组合（Ascend 950PR 可用仿真执行样例）。

#### 4.2.3 源码精读

自定义算子包模式的命令模板（[docs/zh/invocation/quick_op_invocation.md:L36-L49](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md#L36-L49)）：文档以 `bash build.sh --run_example grid_sample eager cust --vendor_name=custom` 为例，逐个解释 `${op}`、`${mode}`、`${pkg_mode}`、`${vendor_name}`、`${soc_version}`、`${experimental}` 六个参数，并注明 graph 模式不指定 pkg_mode 与 vendor_name。

ops-cv 整包模式（[docs/zh/invocation/quick_op_invocation.md:L53-L63](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md#L53-L63)）：命令更短——`bash build.sh --run_example grid_sample eager`，无需 vendor 信息，因为整包样例链接的是 CANN 安装目录下的内置算子库。

执行结果示例（[docs/zh/invocation/quick_op_invocation.md:L137-L162](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md#L137-L162)）：脚本日志会打印 `Start to run examples,name:grid_sample mode:eager`、正在编译的样例文件路径（`../image/grid_sample/examples/test_aclnn_grid_sample2_d.cpp`），随后是样例自身打印的 `resultData[i] is: ...` 输出——这组数值就是你在 4.2.4 实践中要核对的目标。

#### 4.2.4 代码实践

**实践目标**：用一条命令运行仓库中一个真实算子（grid_sample）的 aclnn 样例，并读懂日志。

**操作步骤**：

1. 确认环境已按 u1-l4 完成准备：安装 CANN-toolkit、编译并安装 grid_sample 算子包（例如此前用 `bash build.sh --pkg --soc=ascend910b --ops=grid_sample` 构建过，vendor 为默认 custom）。
2. 在仓库根目录执行：
   ```bash
   bash build.sh --run_example grid_sample eager cust --vendor_name=custom --soc=ascend910b
   ```
3. 观察日志，记录三件事：脚本打印的样例源文件路径、`pkg_mode` 与 `vendor_name` 的回显、`resultData` 各行数值。
4. 再试 graph 模式（不带 pkg_mode）：`bash build.sh --run_example grid_sample graph`，对比两者输出形式差异。

**需要观察的现象**：eager 模式直接打印逐元素 `resultData[i]`；graph 模式结果通常落盘或打印图执行日志（参考 u1-l4 的 geir 样例）。

**预期结果**：eager 输出与文档 [L146-L161](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md#L146-L161) 的 `resultData` 数值一致（0.250000、2.250000、2.000000、8.500000 等）。若不一致，优先检查 `--soc` 是否与算子包编译时相同（回顾 u1-l4 的 error 161001 教训）。本实践依赖真实 NPU 环境，运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`--run_example` 的 `${mode}` 有哪两个取值？分别对应哪种调用方式？
**答案**：`eager`（aclnn 单算子调用）与 `graph`（GE 图模式调用）。

**练习 2**：为什么 graph 模式下不需要指定 `${pkg_mode}` 和 `${vendor_name}`？
**答案**：GE 图引擎根据环境变量（如 `ASCEND_OPP_PATH`）自动加载已安装的算子包，无论是自定义包还是内置包都走同一机制，无需在命令行显式区分（文档 GE 图模式 CMakeLists 一节也说明了这一点，[L493-L495](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md#L493-L495)）。

**练习 3**：执行 experimental 目录下贡献算子的样例要加什么参数？
**答案**：加 `--experimental`，如 `bash build.sh --experimental --run_example grid_sample eager cust --vendor_name=custom`（见文档 [L40-L41](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md#L40-L41)）。

### 4.3 业务应用集成算子：自建 aclnn 调用工程

#### 4.3.1 概念说明

当算子要进入真实业务，快速调用的脚本就不够了——你需要一个可移植、可版本管理的调用工程。官方文档给出了标准三件套：**调用 cpp（样例骨架，u2-l1 已学）+ CMakeLists.txt + run.sh**。本模块聚焦其中最有信息量的部分：**链接哪个算子库**。

#### 4.3.2 核心流程

```text
自建工程目录
├── test_aclnn_xxx.cpp     # 调用代码：Init → 构造 Tensor → 两段式调用 → 同步 → 拷回 → 清理
├── CMakeLists.txt         # 关键分歧点：
│     ├─ 调自定义算子：扫 opp/vendors/* 找到算子包，链接 libcust_opapi.so，头文件在 <vendor>/op_api/include
│     └─ 调内置算子：  头文件在 <cann>/include/aclnnop，链接 libopapi_cv.so
└── run.sh                 # source setenv.bash → cmake → make → 执行
```

#### 4.3.3 源码精读

调用脚本骨架（[docs/zh/invocation/quick_op_invocation.md:L199-L273](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md#L199-L273)）：以 AddExample 为例的十步 main 函数——acl 初始化、构造输入输出 aclTensor、第一段接口拿 workspaceSize、申请 workspace、第二段接口下发、同步、打印结果、逐个销毁。这正是 u2-l1"九步骨架"的官方版。

自定义算子的 CMakeLists（[docs/zh/invocation/quick_op_invocation.md:L309-L339](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md#L309-L339)）：用 `file(GLOB "${VENDORS_DIR}/*")` 在 `opp/vendors` 下探测算子包目录，头文件加入 `${TARGET_SUBDIR}/op_api/include`，链接 `${TARGET_SUBDIR}/op_api/lib/libcust_opapi.so` 并设置 rpath。注意文档注释提醒：存在多个自定义算子包时只会使用其中一个。

内置算子的 CMakeLists（[docs/zh/invocation/quick_op_invocation.md:L374-L385](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md#L374-L385)）：更简单——头文件直接用 `${ASCEND_PATH}/include/aclnnop`，链接 `${ASCEND_PATH}/lib64/libopapi_cv.so`（ops-cv 整包的内置算子库），无需 vendor 探测。

run.sh 模板（[docs/zh/invocation/quick_op_invocation.md:L395-L413](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md#L395-L413)）：先按 `ASCEND_INSTALL_PATH`/`ASCEND_HOME_PATH` 优先级定位 CANN 包并 `source setenv.bash`，然后 `cmake ../ && make`，最后执行 `build/bin` 下的可执行文件。

#### 4.3.4 代码实践

**实践目标**：不用脚本，亲手搭一个最小 aclnn 调用工程，体会"业务集成"与"快速调用"的差别。

**操作步骤**：

1. 在任意目录（建议仓库外，模拟真实业务）新建 `my_app/`，把 [examples/add_example/examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_aclnn_add_example.cpp) 拷贝进去，改名为 `test_aclnn_add_example.cpp`。
2. 按文档 [L282-L343](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md#L282-L343) 抄一份"调用自定义算子"版 CMakeLists.txt（因为你的 AddExample 是自定义算子包安装的）。
3. 抄 [L395-L413](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md#L395-L413) 的 run.sh，把 `${test_aclnn_op_name}` 替换为 `test_aclnn_add_example`。
4. 执行 `bash run.sh`。

**需要观察的现象**：cmake 阶段打印的 `TARGET_SUBDIR` 是否指向你安装的 vendor 目录；链接阶段是否找到 `libcust_opapi.so`。

**预期结果**：输出 `mean result[i] is 2.000000`（1+1=2，与文档 [L426-L428](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md#L426-L428) 一致）。若报找不到算子包，检查 `opp/vendors` 下是否有 `custom/custom_cv` 目录。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：自建工程中，自定义算子与内置算子的 CMakeLists 最核心的两处差异是什么？
**答案**：① 头文件路径——自定义算子用 `<vendor>/op_api/include`（需 GLOB 探测 vendor 目录），内置算子用 `<cann>/include/aclnnop`；② 链接库——自定义算子链 `libcust_opapi.so` 并设 rpath，内置算子链 `libopapi_cv.so`。

**练习 2**：如果把 `ASCEND_HOME_PATH` 指向错误路径会发生什么？
**答案**：run.sh 的回退逻辑会用 `/usr/local/Ascend/cann`（[L396-L402](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/invocation/quick_op_invocation.md#L396-L402)），若该路径也无有效安装，则 `source setenv.bash` 或后续 cmake/链接会失败，报找不到头文件或库。

### 4.4 PyTorch 扩展：fast_kernel_launch_example 工程结构

#### 4.4.1 概念说明

第三类方案解决的问题是：业务代码是 PyTorch 的，希望像调 `torch.add` 一样调自己写的 Ascend C Kernel，而不是在 C++ 工程里手写 aclnn。`examples/fast_kernel_launch_example` 演示了完整做法——README 概括其两大优势（[examples/fast_kernel_launch_example/README.md:L7-L11](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/README.md#L7-L11)）：**单交付件**（一个 cpp 完成算子开发与框架适配）与**高效调用**（`<<<>>>` 语法直接启动核函数）。

它与传统 aclnn 算子工程的本质区别：不走 op_host/op_kernel/算子包那套交付链，而是把 Kernel 直接编进一个 PyTorch 扩展 `.so`，用 PyTorch 的算子分发机制替代 CANN 的算子注册机制。

#### 4.4.2 核心流程

**构建链**：

```text
python3 -m build --wheel -n
  └─ setup.py（ABI3Wheel.run → 先跑 cmake_build 命令）
       └─ CMakeBuildCommand：探测 torch/torch_npu 路径，读 NPU_SOC_VERSION，
          调 cmake -S . -B build && cmake --build
            └─ 顶层 CMakeLists.txt：EXTENSION_MODULE_NAME=ascend_ops，
               add_subdirectory(csrc) 递归收集算子 .cpp 为 OBJECTS_LIST，
               与 extension.cpp 一起链成 _C.abi3.so 并拷入 ascend_ops/ 包目录
  └─ 产出 dist/ascend_ops-1.0.0-cp38-abi3-<arch>.whl → pip install
```

**运行链**：

```text
python: import ascend_ops        # __init__.py 触发 from . import _C → 加载 .so
   → .so 内 TORCH_LIBRARY 静态初始化器执行，算子 schema/Meta/NPU 实现注册进 PyTorch
   → torch.ops.ascend_ops.add(x, y)
   → PyTorch Dispatch：输入在 NPU（PrivateUse1 键）→ add_npu
   → add_npu：Meta 定形状 → calc_tiling_params → add_kernel<<<numBlocks, nullptr, stream>>>
   → OpCommand::RunOpApi 保证与 TorchNPU 的 aclnn 调用时序一致
```

**一个算子的注册四件套**（都在同一个 cpp 里）：

| 件 | 宏/函数 | 作用 |
| --- | --- | --- |
| Schema | `TORCH_LIBRARY_FRAGMENT` | 声明签名 `add(Tensor x, Tensor y) -> Tensor` |
| Meta | `TORCH_LIBRARY_IMPL(..., Meta, ...)` | 推 shape/dtype，支撑 torch.compile 等图加速 |
| Kernel | `__global__ __aicore__ void add_kernel` | Ascend C 核函数（TPipe + 双缓冲流水） |
| NPU 实现 | `TORCH_LIBRARY_IMPL(..., PrivateUse1, ...)` | NPU 后端入口，负责 tiling 与核启动 |

#### 4.4.3 源码精读

**（1）setup.py——Python 侧构建入口。** `CMakeBuildCommand` 在打 wheel 前先驱动 CMake：导入 torch 与 torch_npu 拿到 `Torch_DIR`、`TORCH_NPU_PATH`，从环境变量读 `NPU_SOC_VERSION`（默认 ascend910b），拼出 cmake 配置与构建命令（[examples/fast_kernel_launch_example/setup.py:L93-L127](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/setup.py#L93-L127)）。`ABI3Wheel` 强制 wheel 标签为 `cp38/abi3`，使一个包兼容 Python ≥3.8，并在 `run()` 里先执行 `cmake_build` 再打 wheel（[setup.py:L65-L77](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/setup.py#L65-L77)）。

**（2）顶层 CMakeLists.txt——收集与产物。** 它处理 NPU_SOC_VERSION 的默认值/兼容旧名 NPU_ARCH（[examples/fast_kernel_launch_example/CMakeLists.txt:L27-L35](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/CMakeLists.txt#L27-L35)），把扩展模块名定为 `ascend_ops`（[CMakeLists.txt:L37](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/CMakeLists.txt#L37)），`add_subdirectory(csrc)` 递归收集算子源文件到 `OBJECTS_LIST`（[CMakeLists.txt:L79-L80](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/CMakeLists.txt#L79-L80)；递归逻辑在 [csrc/CMakeLists.txt:L11](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/csrc/CMakeLists.txt#L11) 的 `recursive_add_subdirectory()`），最终把所有对象与 extension.cpp 链成共享库 `_C.abi3.so` 并 POST_BUILD 拷贝进 Python 包目录（[CMakeLists.txt:L83-L105](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/CMakeLists.txt#L83-L105)）。

**（3）csrc/extension.cpp——最薄的 Python 入口。** 整个文件只有一个 `PyInit__C`，创建一个空模块（[csrc/extension.cpp:L18-L35](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/csrc/extension.cpp#L18-L35)）。它的注释点破设计意图：Python `import` 触发 `.so` 加载，唯一目的是让链在同一 `.so` 里的各算子文件中 `TORCH_LIBRARY` 静态初始化器执行，从而完成注册——Python 侧不需要任何显式绑定函数。

**（4）add.cpp——注册四件套落地。** Schema 与 Meta 注册（[csrc/add/ascend910b/add.cpp:L32-L46](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/csrc/add/ascend910b/add.cpp#L32-L46)）：`m.def("add(Tensor x, Tensor y) -> Tensor")` 声明算子，`add_meta` 校验 shape 一致后 `empty_like` 出输出张量并注册到 Meta 键。tiling 计算（[add.cpp:L53-L79](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/csrc/add/ascend910b/add.cpp#L53-L79)）：通过 `PlatformAscendCManager` 查询 UB 大小与 AIV 核数，算出 `numBlocks`（用的核数）、`blockLength`（每核元素数）、`tileSize`（UB 除以流水深度与缓冲数）。核函数（[add.cpp:L84-L199](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/csrc/add/ascend910b/add.cpp#L84-L199)）：标准 Ascend C 三段流——CopyIn（`DataCopyPad` GM→UB）、Compute（`AscendC::Add`）、CopyOut，配合 `TQue` 深度 2 的双缓冲。NPU 入口与分发（[add.cpp:L203-L259](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/csrc/add/ascend910b/add.cpp#L203-L259)）：`add_npu` 用 `OptionalDeviceGuard` 绑定设备上下文、调 meta 定输出、取 TorchNPU 当前 stream，再按 dtype 用 `AT_DISPATCH_SWITCH` 分发到模板化的 `add_kernel<<<numBlocks, nullptr, stream>>>`（L233），整个调用包在 `at_npu::native::OpCommand::RunOpApi("Add", acl_call)`（L253）里以保证与 TorchNPU aclnn 调用时序一致，最后经 `TORCH_LIBRARY_IMPL(..., PrivateUse1, ...)` 注册（L259），使 NPU 上的输入自动路由到该实现。

**（5）Python 侧门面。** 包初始化 `from . import _C` 触发注册（[ascend_ops/__init__.py:L17-L22](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/ascend_ops/__init__.py#L17-L22)）；`ops.py` 把 `torch.ops.ascend_ops.upsample_nearest3d` 包装成带类型标注的 Python 函数（[ascend_ops/ops.py:L20-L22](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/ascend_ops/ops.py#L20-L22)），这是给业务暴露"类原生 API"的推荐形态。README 的 Quick Start 则展示了三行核心调用：`x.npu()` 上板、`torch.ops.ascend_ops.add(x, y)` 调用、与 CPU 结果 `allclose` 对比（[examples/fast_kernel_launch_example/README.md:L58-L77](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/README.md#L58-L77)）。

**（6）验证。** pytest 用例按 shape × dtype 参数化，NPU 结果与 CPU ATen 的 `a + b` 对比（[tests/add/test_add.py:L72-L103](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/tests/add/test_add.py#L72-L103)）；另有专门的存在性测试 `test_add_interface_exist` 守护"schema 与 C++ 注册签名不一致导致算子无法从 torch.ops 发现"这一常见故障（[test_add.py:L19-L40](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/tests/add/test_add.py#L19-L40)）。

#### 4.4.4 代码实践

**实践目标**：亲手构建安装 fast_kernel_launch_example，并从源码层面解释"`import ascend_ops` 之后为什么 `torch.ops.ascend_ops.add` 就能用"。

**操作步骤**：

1. 环境准备：gcc 9.4.0+、python 3.8+、torch≥2.6.0、对应版本 TorchNPU（[README.md:L13-L20](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/README.md#L13-L20)）。
2. 进入 `examples/fast_kernel_launch_example`，依次执行（[README.md:L23-L46](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/README.md#L23-L46)）：
   ```bash
   python3 -m pip install -r requirements.txt
   export NPU_SOC_VERSION=ascend910b          # 按实际板卡选 ascend910b / ascend910_93 / ascend950
   python3 -m build --wheel -n
   python3 -m pip install dist/*.whl --force-reinstall --no-deps
   ```
3. 运行 README Quick Start 的验证脚本（构造两个 `torch.randn(10, 32).npu()` 相加）。
4. 运行 pytest：`python3 -m pytest tests/add/test_add.py`。
5. 写一段文字（5-8 句）回答：`csrc/extension.cpp` 里没有任何 `add` 相关代码，为什么 import 后 add 就被注册了？

**需要观察的现象**：wheel 文件名中的 `cp38-abi3` 标签；`assert torch.allclose(...)` 通过打印 `Verification successful!`；pytest 中 19 个 shape × 3 个 dtype 的组合全部通过。

**预期结果**：步骤 5 的答案应包含——`import ascend_ops` 触发 `__init__.py` 的 `from . import _C`，加载 `_C.abi3.so` 时操作系统执行其中的静态初始化器，`add.cpp` 里三个 `TORCH_LIBRARY_*` 宏注册的 schema/Meta/PrivateUse1 实现随之进入 PyTorch 算子表，故 `torch.ops.ascend_ops.add` 可用。**待本地验证**（需要 NPU 环境与 TorchNPU）。

#### 4.4.5 小练习与答案

**练习 1**：add 算子为什么要注册 Meta 实现？不注册会怎样？
**答案**：Meta 函数让框架在真正计算前知道输出的 shape/dtype 与所需空间，从而支持 torch.compile、AutoGrad、AclGraph 等图加速特性（README [L133-L139](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/README.md#L133-L139)）。不注册则这些图能力下无法推导输出，算子只能在 eager 直接调用。

**练习 2**：`add_kernel<<<numBlocks, nullptr, stream>>>` 三个参数各是什么含义？
**答案**：`numBlocks` 是启动的核数（tiling 算出的并行块数），`nullptr` 是共享内存位置参数（NPU 无对应概念，占位），`stream` 是 NPU 流，取自 TorchNPU 的 `getCurrentNPUStream()`，保证与 PyTorch 的异步语义一致。

**练习 3**：为什么 `add_npu` 里要把核启动包进 `OpCommand::RunOpApi("Add", acl_call)`？
**答案**：源码注释（[add.cpp:L252-L253](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/csrc/add/ascend910b/add.cpp#L252-L253)）说明这是为了保证本次直接核启动的执行时序与 TorchNPU 走 aclnn 接口时一致（如统一的任务下发与同步上下文），避免与框架内其他 NPU 任务乱序。

**练习 4**：若要为该工程新增一个 `sub` 算子，目录应怎么组织？
**答案**：在 `csrc/sub/ascend910b/` 下建 `CMakeLists.txt`（内容为 `add_sources("--npu-arch=dav-2201")`，见 [csrc/add/ascend910b/CMakeLists.txt:L11](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/fast_kernel_launch_example/csrc/add/ascend910b/CMakeLists.txt#L11)）和 `sub.cpp`（四件套），重新构建 wheel 安装即可，顶层 CMake 会递归收集。

## 5. 综合实践

**任务：为同一个算子走通三条调用路径，做一份横向对比报告。**

以仓库中的 `grid_sample`（或任一你已能编译的算子）为对象：

1. **路径一（快速调用）**：`bash build.sh --run_example grid_sample eager cust --vendor_name=custom`，记录命令行长度、是否写了任何工程代码、输出形式。
2. **路径二（业务集成）**：把 `image/grid_sample/examples/` 下的 test_aclnn 样例拷出，按 4.3 的模板自建 cpp + CMakeLists + run.sh 工程并跑通，记录你实际写的文件清单与每个文件行数。
3. **路径三（PyTorch 风格）**：通读 fast_kernel_launch_example，写 200 字说明如果要把 grid_sample 做成 `torch.ops.ascend_ops.grid_sampler2d`，需要在 `csrc/` 下新建哪些文件、实现哪四个注册件（不必真的实现，画出文件结构即可）。
4. 产出一张对比表：三条路径在"工程成本、是否依赖算子包、调用方语言、适用场景、可移植性"五个维度的差异，并给出你的选型结论。

无 NPU 环境时，路径一/二的运行结果标注「待本地验证」，路径三纯源码阅读即可完成。

## 6. 本讲小结

- 官方把算子调用组织成"两类场景 × 三种方式"：快速调用（`build.sh --run_example`，零工程成本）与业务集成（自建工程，三种 API 面：PyTorch / aclnn / GE 图模式）。
- `--run_example op mode [pkg_mode] [--vendor_name --soc --experimental]` 一条命令完成样例的编译与执行；graph 模式无需 pkg_mode，GE 引擎按环境变量自动加载算子包。
- 自建 aclnn 工程的核心分歧在链接对象：自定义算子链 vendors 目录下的 `libcust_opapi.so`，内置算子链 `libopapi_cv.so`；静态库模式则用 g++ 手工链接 `cann_cv/math/legacy` 三个静态库。
- fast_kernel_launch_example 用 PyTorch Extension 承载 Ascend C Kernel：`setup.py` 驱动 CMake → 递归收集 `csrc/<op>/<soc>/*.cpp` → 链成 `_C.abi3.so` 放进 Python 包。
- 算子注册四件套在单文件内完成：`TORCH_LIBRARY_FRAGMENT`（schema）、Meta 实现（shape 推导）、`__global__ __aicore__` 核函数、`PrivateUse1` 实现（tiling + `<<<>>>` 核启动）；`extension.cpp` 的空 `PyInit__C` 只为触发这些静态注册。
- 选型口诀：验证用快速调用，C++ 业务用 aclnn 自建工程，PyTorch 训推用扩展集成。

## 7. 下一步学习建议

- 下一讲（u2-l4）将走向第三种调用方式的源码层：GE 图模式调用算子，阅读 `test_geir_add_example.cpp` 的构图与 Session 执行流程，并与本讲的 aclnn 路径对比。
- 若你对 fast_kernel_launch_example 的 Kernel 实现感兴趣，可以提前翻阅 `add.cpp` 中 `TPipe`/`TQue`/`DataCopyPad` 的用法，它们将在第四单元（u4-l1 Ascend C Kernel 基础）系统展开。
- 想深入了解 PyTorch 分发机制（Meta / PrivateUse1 / Dispatch 键），可结合 PyTorch 官方 torch.library 文档与 `TORCH_LIBRARY_IMPL` 宏对照阅读。
