# 测试与 CI

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 TensorRT Edge-LLM 的**三层测试架构**分别是什么、各自验证什么、跑在哪一层；
- 理解 `tests/conftest.py` 如何用一条 `--priority` 命令行参数把 YAML 测试清单翻译成 pytest 的参数化用例，并严格按清单声明顺序执行；
- 看懂 `tests/test_lists/*.yml` 这类清单文件如何按**目标 GPU/设备**切分用例（`l0_pipeline_a30` 跑在 A30、`l0_pipeline_orin` 跑在 Jetson Orin 等）；
- 掌握 C++ 单元测试（`unittests/`）如何被 CMake 编译成单一 `unitTest` 可执行文件、如何用 `--gtest_filter` 单跑某个用例；
- 解释为什么一条模型改动 PR 必须附带 **export→build→inference** 三段式验证证据。

本讲是「特性与扩展」单元的收尾篇，承接 u1-l3（构建系统）与 u2-l6（导出 CLI 编排），把视角从「怎么写代码」转到「怎么证明代码没坏」。

## 2. 前置知识

在进入测试代码前，先用通俗语言澄清几个概念。

- **三层测试**。这是软件工程里常见的金字塔结构：最底层是又快又便宜的单元测试（unit test，只验证一个函数/一个 kernel）；中间是组件测试（验证一个插件或一个模块）；最顶层是又慢又贵的端到端集成测试（端到端跑通 export→build→inference）。越靠下层用例越多、跑得越快；越靠上层用例越少、覆盖越像真实场景。本项目的「三层」对应：C++ GTest、Python 组件测试、YAML 驱动的流水线测试。

- **pytest 的参数化（parametrize）**。pytest 允许把同一个测试函数跑很多遍，每遍喂不同参数。本项目把「模型名-精度-序列长度」这种字符串当成参数，一个测试函数就能覆盖几十种模型配置。

- **pytest 的 hook（钩子）**。pytest 在收集、执行用例前后会调用一批「钩子函数」。`conftest.py` 就是放这些钩子的地方。本项目用 `pytest_generate_tests`（生成参数）和 `pytest_collection_modifyitems`（重排用例）两个钩子，把 YAML 清单注入 pytest。

- **GTest**。Google 的 C++ 单元测试框架。用 `TEST(...)` 或 `TEST_F(...)` 宏声明一个用例，用 `EXPECT_EQ`/`ASSERT_EQ` 等宏做断言。一个可执行文件里可以塞成百上千个用例，靠 `--gtest_filter` 筛选只跑其中一部分。

- **Golden reference（黄金参考）**。CUDA kernel 测试的常见套路：另写一份简单但绝对正确的 CPU/朴素实现作为「标尺」，把 GPU kernel 的输出和它逐元素比对。本项目 `eagleAcceptTests.cpp` 就这么做。

- **export→build→inference**。这是本项目的三段式流水线（见 u1-l2）：检查点 → Python 导出 ONNX → C++ 构 engine → C++ 推理。任何模型改动的正确性，最终都要靠「这条链能跑通且输出对」来证明。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `tests/conftest.py` | pytest 全局配置：环境变量解析、命令行参数（`--priority` 等）、把 YAML 清单翻译成参数化用例的钩子、按 YAML 顺序重排用例。 |
| `tests/test_lists/l0_pipeline_a30.yml` | 一个具体的测试清单样本，列出 A30 GPU 上要跑的 build/inference/server 用例。 |
| `tests/README.md` | 测试使用文档：环境变量、运行命令、可用清单、参数字符串格式、远程执行。 |
| `unittests/eagleAcceptTests.cpp` | C++ GTest 样本：测试 EAGLE 投机解码的「接受」kernel，用 CPU 参考实现逐元素比对。 |
| `unittests/engineExecutorTest.cpp` | C++ GTest 样本：测试引擎执行器的 `BindingSnapshot` 相等比较，**不需要真实 TRT engine**。 |
| `CMakeLists.txt`（顶层） | 定义 `BUILD_UNIT_TESTS` 选项与 `unitTest` 可执行目标，用 `GLOB_RECURSE` 收集 `unittests/` 下所有 `.cpp/.cu`。 |
| `tests/defs/test_common.py` | Python 流水线测试里的「公共前置」：`test_build_project` 编译整个项目、`test_unit_tests` 跑 `./unitTest`。 |

## 4. 核心概念与源码讲解

### 4.1 测试三层架构与 conftest（YAML 驱动的测试选择）

#### 4.1.1 概念说明

TensorRT Edge-LLM 的测试体系是一个金字塔，从底到顶分三层：

1. **C++ GTest 单元测试**（`unittests/`）：编译成单一可执行文件 `build/unitTest`，验证单个 kernel、单个数据结构（如 `Tensor`、`TensorMap`、`BindingSnapshot`）、单个管理器（如 `HybridCacheManager`、`LoraManager`）。快、便宜、需要 CUDA GPU 但**不需要**真实模型或 engine。

2. **Python 组件/插件测试**（`tests/python-unittests/`）：用 pytest 风格写，验证 TensorRT 插件（attention/mamba/gdn 等）相对 PyTorch 参考实现的正确性，以及实验性服务端的工具调用、logit bias 等纯 Python 逻辑。清单见 `tests/test_lists/l0_python_ut.yml`。

3. **YAML 驱动的流水线集成测试**（`tests/defs/`）：端到端 `export → build → inference`，按目标设备切分成几十个清单文件（`l0_pipeline_a30.yml`、`l0_pipeline_orin.yml` 等）。慢、贵、需要真实模型检查点与目标 GPU。

这三层里，**第二、三层都跑在 pytest 框架下**，而 `tests/conftest.py` 是 pytest 的「中央调度器」：它负责读环境、注册命令行选项、把 YAML 清单翻译成参数化用例、并强制按 YAML 声明顺序执行。理解了 conftest，就理解了第二、三层怎么被选中和运行。

#### 4.1.2 核心流程

conftest 的工作分四块，按 pytest 生命周期排列：

1. **收集前：解析命令行**。`pytest_addoption` 注册 `--priority`、`--test-param`、`--execution-mode` 等选项。
2. **收集前：读环境变量**。`EnvironmentConfig.from_environment()` 把 `LLM_SDK_DIR`/`ONNX_DIR`/`ENGINE_DIR` 等环境变量解析成强类型配置。
3. **收集时：参数生成**。`pytest_generate_tests` 钩子读 YAML 清单，把清单里 `[...]` 中的参数字符串喂给测试函数的 `test_param` 形参。
4. **收集后：重排**。`pytest_collection_modifyitems` 钩子丢弃 YAML 里没列出的用例，并按 YAML 出现顺序重新排列保留的用例。

关键流程的伪代码如下：

```
用户执行: pytest --priority=l0_pipeline_a30
   │
   ├─ pytest_addoption: 注册 --priority（默认 None）
   │
   ├─ pytest_generate_tests(metafunc):
   │     priority = config.getoption("--priority")          # "l0_pipeline_a30"
   │     yaml = _get_test_list_file(priority)               # 读 tests/test_lists/l0_pipeline_a30.yml
   │     从 yaml['tests'] 里挑出与本测试函数同名的条目
   │     把条目 [...] 里的参数收集成 list
   │     metafunc.parametrize("test_param", 该 list)
   │
   └─ pytest_collection_modifyitems(config, items):
         再次读同一份 yaml
         只保留 yaml 里列出的 item，丢弃其余
         按 yaml 声明顺序重排 items[:]
```

#### 4.1.3 源码精读

**命令行选项注册**。`--priority` 是整套机制的入口，它本身只是一个普通字符串，默认 `None`：

`pytest_addoption` 注册 `--priority` 选项，注释写「Test priority level (l0, l1, etc.)」——见 [tests/conftest.py:299-326](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/conftest.py#L299-L326)。同时注册的还有 `--test-param`（绕过 YAML，直接喂一个参数串）和 `--execution-mode`（local/remote 远程跑）。

**YAML 文件定位**。核心是把 priority 字符串映射到一个 `.yml` 文件路径。`_get_test_list_file` 做这件事，并带进程级缓存 `_test_config_cache` 避免重复读盘：

`_get_test_list_file` 把 `l0_pipeline_a30` 翻译成 `tests/test_lists/l0_pipeline_a30.yml`——见 [tests/conftest.py:407-422](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/conftest.py#L407-L422)。若 priority 已带 `.yml`/`.yaml` 后缀则直接当路径用；找不到文件返回 `None`（不报错，交由后续钩子静默跳过）。缓存声明在 [tests/conftest.py:349](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/conftest.py#L349)。

**参数生成钩子**。`pytest_generate_tests` 是 pytest 收集阶段的标准钩子，对每个带 `test_param` 形参的测试函数都会被调用一次。它的逻辑是：先看 `--test-param` 是否直接给了参数（给了就完全绕过 YAML），否则按 priority 读 YAML，遍历 `yaml['tests']`，把其中与本测试函数同名的条目的 `[参数]` 部分收集起来传给 `metafunc.parametrize`：

`pytest_generate_tests` 解析 YAML 条目里的 `模块::函数[参数]` 三段式，按模块名/类名/函数名匹配当前测试函数，提取 `[...]` 内的参数——见 [tests/conftest.py:425-472](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/conftest.py#L425-L472)。条目格式形如 `tests/defs/test_llm_pipeline.py::test_engine_build[Qwen2.5-0.5B-Instruct-fp16-mxsl4096-mxbs4-mxil2048]`，它先用 `::` 拆出文件路径与函数部分，再把 `[...]` 里的 `Qwen2.5-0.5B-Instruct-fp16-mxsl4096-mxbs4-mxil2048` 作为 `test_param` 注入。

**重排与过滤钩子**。光生成参数还不够——pytest 默认按发现顺序跑，可能跑出 YAML 之外的用例或顺序错乱。`pytest_collection_modifyitems` 在收集完成后兜底：只保留 YAML 里出现的 item，并严格按 YAML 声明顺序排列：

`pytest_collection_modifyitems` 遍历 YAML 的 `tests` 列表，按声明顺序把匹配的 item 放进 `ordered`，最后 `items[:] = ordered` 原地替换——见 [tests/conftest.py:475-514](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/conftest.py#L475-L514)。当走 `--test-param` 旁路时，它改调 `_filter_direct_model_quantization_items` 去重（同一参数被多个量化测试函数收集时只留最匹配的一个）。

> 这一对钩子是「**清单即真相**」设计：YAML 清单既是「跑哪些用例」的白名单，又是「按什么顺序跑」的编排表。开发者新增模型只需往清单加一行 `模块::函数[参数]`，无需改任何 Python 代码。

**环境配置与 fixture**。`EnvironmentConfig.from_environment()` 把必填的 `LLM_SDK_DIR`、`ONNX_DIR` 与可选的 `ENGINE_DIR`/`TRT_PACKAGE_DIR` 等读成强类型 dataclass，并对缺失的必填项抛清晰错误：

`from_environment` 解析环境变量，并对 `LLM_MODELS_DIR`/`EDGELLM_DATA_DIR` 给出一组默认路径做兜底探测——见 [tests/conftest.py:55-116](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/conftest.py#L55-L116)。配合 fixture `env_config`（session 级）与 `executable_files`（给出 `build/` 下各可执行文件路径），测试函数就能以依赖注入方式拿到环境——见 [tests/conftest.py:171-202](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/conftest.py#L171-L202)。

#### 4.1.4 代码实践

**实践目标**：亲手追踪一条 `--priority=l0_pipeline_a30` 是如何变成「跑这几个用例」的。

**操作步骤**：

1. 打开 [tests/test_lists/l0_pipeline_a30.yml](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/test_lists/l0_pipeline_a30.yml)，记下第一条 `tests/defs/test_llm_pipeline.py::test_engine_build[Qwen2.5-0.5B-Instruct-fp16-mxsl4096-mxbs4-mxil2048]`。
2. 打开 [tests/conftest.py:407-422](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/conftest.py#L407-L422)，确认 `l0_pipeline_a30` 会被拼成 `tests/test_lists/l0_pipeline_a30.yml`。
3. 打开 [tests/conftest.py:425-472](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/conftest.py#L425-L472)，确认当 pytest 收集到 `test_engine_build` 函数时，钩子会从该 YAML 条目里抽出 `Qwen2.5-0.5B-Instruct-fp16-mxsl4096-mxbs4-mxil2048` 作为 `test_param`。
4. 打开 [tests/defs/test_llm_pipeline.py:39-52](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/defs/test_llm_pipeline.py#L39-L52)，确认 `test_engine_build` 的第一行就是 `TestConfig.from_param_string(test_param, ...)`，即把这个字符串参数解析成构建配置。
5. 若本机无 GPU，用 pytest 的 collect-only 模式只看选中了哪些用例、不真正执行（**待本地验证**）：
   ```bash
   export LLM_SDK_DIR=$(pwd) ONNX_DIR=/tmp/onnx ENGINE_DIR=/tmp/eng
   pytest --priority=l0_pipeline_a30 --collect-only -q
   ```

**需要观察的现象**：`--collect-only` 输出的用例 ID 列表，应当与 `l0_pipeline_a30.yml` 里列出的条目**一一对应且顺序一致**，且不含该清单之外的任何用例。

**预期结果**：你会看到形如 `test_llm_pipeline.py::TestLLMPipeline::test_engine_build[Qwen2.5-0.5B-Instruct-fp16-mxsl4096-mxbs4-mxil2048]` 的若干行，顺序与 YAML 相同。如果清单里没列某个模型，它就不会出现——这正是「清单即白名单」的效果。

#### 4.1.5 小练习与答案

**练习 1**：如果不传 `--priority`，`_get_test_list_file` 返回什么？后续 `pytest_generate_tests` 会怎样？

**参考答案**：不传时 `--priority` 默认 `None`（见 [conftest.py:301-304](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/conftest.py#L301-L304)）。注意 `pytest_generate_tests` 里读的是 `getoption("--priority", "l0")`（[conftest.py:436](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/conftest.py#L436)），所以默认会去找 `tests/test_lists/l0.yml`。若该文件不存在，`_get_test_list_file` 返回 `None`，钩子直接 `return`，于是没有任何参数被注入，带 `test_param` 的测试函数因缺少参数而被 pytest 跳过（或报 fixture 缺失）。

**练习 2**：`--test-param` 与 `--priority` 同时存在时，谁优先？为什么？

**参考答案**：`--test-param` 优先。在 `pytest_generate_tests` 里（[conftest.py:430-434](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/conftest.py#L430-L434)），只要 `--test-param` 非空就 `metafunc.parametrize` 后立刻 `return`，**完全跳过 YAML 读取**。这样开发者可以不写 YAML、直接单跑一个模型配置做调试。

### 4.2 test_lists：按设备/GPU 参数化的测试清单

#### 4.2.1 概念说明

`tests/test_lists/` 目录下有四十多个 `.yml` 文件，每个文件是一份「在某个目标硬件上要跑哪些用例」的清单。文件名本身就是设备标识符：

- `l0_pipeline_a30.yml` → A30 GPU（Ampere，数据中心卡）
- `l0_pipeline_orin.yml` → Jetson Orin（边缘 SoC，SM87）
- `l0_pipeline_rtx5080.yml` → RTX 5080（Blackwell 消费卡）
- `l0_pipeline_thor_1.yml` / `l0_pipeline_thor_2.yml` → NVIDIA DRIVE Thor（汽车 SoC）
- `l0_pipeline_jedha.yml` → Jedha（大模型 + 精度 + EAGLE）
- `l0_pipeline_b100.yml` → B100（Blackwell 数据中心）
- `l0_pipeline_spark.yml` → DGX Spark（GB10）

前缀 `l0`/`l1` 是优先级层级：`l0` 是每次提交都要跑的「门槛」用例（build + 基础 inference + 单测），`l1` 是更重的扩展用例（更多模型、EAGLE、VLM、精度对比）。`--priority` 的「priority」一词即来源于此。

为什么按设备切分？因为 **engine 不可跨 GPU 型号迁移**（见 u4-l1）：A30 上构建的 engine 拿到 Orin 上跑不了。同时不同设备的显存、SM 架构、量化精度支持也各异（Jetson Orin 走 FP16/INT8/INT4，Thor/Spark 走 FP8/NVFP4）。所以每个设备需要一份自己的、针对该设备能力裁剪过的用例清单。

#### 4.2.2 核心流程

一份清单的结构非常简单——一个顶层 `tests:` 列表，每行是一个 `模块::函数[参数]` 字符串：

```yaml
tests:
  - tests/defs/test_common.py::test_build_project
  - tests/defs/test_common.py::test_unit_tests
  - tests/defs/test_llm_pipeline.py::test_engine_build[Qwen2.5-0.5B-Instruct-fp16-mxsl4096-mxbs4-mxil2048]
  - tests/defs/test_llm_pipeline.py::test_inference[Qwen2.5-0.5B-Instruct-fp16-mxsl4096-mxbs4-mxil2048-llm_basic]
```

参数字符串本身是一份「微型配置」，编码了模型、精度、引擎范围等信息。其格式在 README 里有明确定义：

```
Runtime: ModelName-Precision-[LmHeadPrecision-]MaxSeqLen-MaxBatchSize-MaxInputLen-[Additional-Params]
```

例如 `Qwen2.5-0.5B-Instruct-fp16-mxsl4096-mxbs4-mxil2048` 表示：模型 Qwen2.5-0.5B-Instruct、精度 fp16、最大序列长 4096、最大 batch 4、最大输入长 2048。

选择哪份清单完全由 `--priority` 决定，链路是：

```
pytest --priority=l0_pipeline_a30
   → conftest._get_test_list_file("l0_pipeline_a30")
   → tests/test_lists/l0_pipeline_a30.yml
   → 这份清单里的用例才被参数化、被执行
```

#### 4.2.3 源码精读

**清单样本**。`l0_pipeline_a30.yml` 是 A30 上的「门槛」清单，前两条总是 `test_build_project`（编译整个项目）与 `test_unit_tests`（跑 C++ 单测），随后是若干模型的 build+inference 配对，以及实验性服务端的 build/inference/streaming/HLAPI 用例：

`l0_pipeline_a30.yml` 完整清单——见 [tests/test_lists/l0_pipeline_a30.yml:1-22](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/test_lists/l0_pipeline_a30.yml#L1-L22)。注意 build 与 inference 是**成对出现**的（先 `test_engine_build[...]` 再 `test_inference[...]`，参数前缀一致），体现了「先建引擎再推理」的依赖。

**可用清单全集**。README 的「Available Test Suites」一节列出了各清单对应的设备：

README 列出 8 个核心清单及其目标设备——见 [tests/README.md:57-66](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/README.md#L57-L66)。例如 `l0_pipeline_orin.yml - Pipeline tests (Jetson Orin)`、`l0_pipeline_thor_2.yml - Pipeline tests (Drive Thor 2, EAGLE)`。

**运行命令**。README 给出的标准运行方式就是 `--priority=<清单名>`：

README 的「Run Tests」给出 `pytest --priority=l0_pipeline_a30 -v`——见 [tests/README.md:42-47](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/README.md#L42-L47)。

**参数字符串格式**。README 把 export 与 runtime 两种格式分别定义，并解释了 `mxsl`/`mxbs`/`mxil` 等缩写（max seq len / max batch size / max input len）以及 VLM 专用的 `mnit`/`mxit`/`mxpiit`（min/max image tokens）：

参数字符串格式定义——见 [tests/README.md:67-113](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/README.md#L67-L113)。

**远程执行**。边缘设备（如 Jetson Orin）算力弱、不便在上面跑全套 Python 导出，所以测试框架支持把命令通过 SSH 发到远端设备执行。`--execution-mode=remote` 配合 `--remote-host` 等参数实现：

README 的「Remote Execution」给出在 Jetson Orin 上远程跑清单的命令——见 [tests/README.md:169-180](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/README.md#L169-L180)。对应的 Python 端 fixture `remote_config` 在 [tests/conftest.py:205-247](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/conftest.py#L205-L247)，从命令行或环境变量 `BOARD_HOST`/`BOARD_USER`/`BOARD_PASSWORD_NVKS`/`REMOTE_WORKSPACE` 取值。

#### 4.2.4 代码实践

**实践目标**：把清单文件名与目标硬件对应起来，并理解为什么 A30 清单里的模型参数在 Orin 清单里可能不同。

**操作步骤**：

1. 用 `ls tests/test_lists/` 列出全部清单（共四十多个）。
2. 对照 [tests/README.md:57-66](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/README.md#L57-L66)，把每个清单名映射到一个设备。
3. 打开 [tests/test_lists/l0_pipeline_a30.yml](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/test_lists/l0_pipeline_a30.yml)，挑出参数串 `Qwen3-VL-2B-Instruct-INT4-AWQ-int4_awq-mxsl8192-mxbs2-mxil8192-mnit128-mxit8192-mxpiit512`，逐段拆解：模型 `Qwen3-VL-2B-Instruct`、量化 `int4_awq`、最大序列长 8192、最大输入长 8192、图像 token 范围 128–8192（单图上限 512）。
4. 拆解参数串 `Qwen2.5-0.5B-Instruct-fp16-mxsl4096-mxbs4-mxil2048-mxlr64`，指出 `mxlr64` 是什么（提示：见 README 的 `mxlr64 - Max LoRA rank`）。

**需要观察的现象**：不同清单里的同一模型，参数可能不同（如 Orin 上 `mxbs` 更小、精度可能用 `int4`/`int8` 而非 `fp8`，因为 Orin 不支持 FP8）。

**预期结果**：你能画出一张「清单名 → 设备 → 典型精度」的对照表。例如 `l0_pipeline_a30` → A30 → fp16/fp8/int4；`l0_pipeline_orin` → Jetson Orin → fp16/int8/int4（无 fp8）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `l0_pipeline_thor_2.yml` 的注释里特别标注「EAGLE」？这和设备能力有什么关系？

**参考答案**：清单名后的括号注释点出该清单的特色用例集。Thor 2 是较新的汽车 SoC（Blackwell 系），算力与显存足以承载投机解码（EAGLE 需要 base + draft 两套引擎、双倍 KV 缓存），所以这份清单专门包含 EAGLE 用例；而较弱的设备（如 Orin）的清单可能只跑 vanilla 解码。清单按设备能力裁剪，避免在不支持的设备上跑注定失败的用例。

**练习 2**：`l0` 与 `l1` 前缀的区别是什么？一条 PR 至少要让哪一档通过？

**参考答案**：`l0` 是门槛级（每次提交必跑：build + 单测 + 基础 inference），`l1` 是扩展级（更多模型、EAGLE、VLM、精度对比，更慢）。一条 PR 至少应让与改动相关的 `l0` 清单在对应设备上全绿；`l1` 视改动范围按需跑。这与本讲综合实践中「最低验证证据」的要求一致。

### 4.3 unittests：C++ GTest 单元测试

#### 4.3.1 概念说明

`unittests/` 目录下有五十多个 `.cpp` 和 `.cu` 文件，每个文件包含若干 GTest 用例，覆盖 kernel、数据结构、管理器等底层组件。这些文件**不是**各自编译成独立可执行文件，而是被 CMake 用 `GLOB_RECURSE` 一把抓起、链接成**单一的 `build/unitTest`** 可执行文件。

这种「单可执行文件装全部用例」的设计有两个好处：

1. **链接一次，跑全部**：避免为每个测试文件维护一个 CMake 目标。
2. **统一过滤**：用 `./unitTest --gtest_filter=EagleAcceptTest.*` 就能只跑某一类用例。

它由 `test_common.py::test_unit_tests` 在流水线测试里驱动执行（即 YAML 清单里的第二条 `test_unit_tests`），所以 C++ 单测其实是被 Python 测试框架「包」起来跑的——这正好把三层测试串了起来：**Python pytest 调用 C++ GTest**。

#### 4.3.2 核心流程

C++ 单测的构建与运行链路：

```
cmake .. -DBUILD_UNIT_TESTS=ON          # 打开单测选项
   │
   ├─ CMake: GLOB_RECURSE 收集 unittests/*.cpp + *.cu
   ├─ CMake: add_subdirectory(3rdParty/googletest)  # 拉入 gtest
   ├─ CMake: add_executable(unitTest <所有源文件>)
   ├─ CMake: target_link_libraries(... gtest gtest_main edgellmCore ...)
   │           # gtest_main 提供默认 main()，内部调 RUN_ALL_TESTS()
   └─ make → build/unitTest

运行: cd build && ./unitTest                    # 跑全部
运行: ./unitTest --gtest_filter=EagleAcceptTest.*   # 只跑某一类
```

C++ 单测里最常见的两种验证模式：

- **Golden reference 比对**：为被测 kernel 写一份朴素 CPU 实现，把 GPU 输出与 CPU 输出逐元素比对（`eagleAcceptTests.cpp`）。
- **纯逻辑断言**：不依赖 GPU/engine，只验证某个数据结构的运算（`engineExecutorTest.cpp` 的 `BindingSnapshot` 相等比较）。

#### 4.3.3 源码精读

**CMake 构建**。顶层 `CMakeLists.txt` 用 `BUILD_UNIT_TESTS` 选项（默认 `OFF`）控制是否编译单测。开启后，拉入 googletest 子模块，glob 收集所有源文件，链接 `gtest` + `gtest_main`（后者提供 `main()`）+ `edgellmCore`：

`unitTest` 目标定义——见 [CMakeLists.txt:188-208](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L188-L208)。关键三行：`file(GLOB_RECURSE UNIT_TESTS_SRCS .../*.cpp .../*.cu)`（[L193-L194](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L193-L194)）、`add_executable(unitTest ${UNIT_TESTS_SRCS})`（[L195](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L195)）、`target_link_libraries(unitTest PRIVATE gtest gtest_main edgellmCore ...)`（[L201](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L201)）。注意它还定义了 `PROJECT_ROOT_DIR` 宏（[L199-L200](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L199-L200)），让测试能定位 `unittests/resources/` 下的资源文件。

**Python 驱动**。`test_common.py::test_unit_tests` 把 `./unitTest` 包成一个 pytest 用例：在 `build/` 目录下执行 `./unitTest`，失败则 `pytest.fail`：

`test_unit_tests` 用 `run_command` 执行 `cd build && ./unitTest`——见 [tests/defs/test_common.py:159-179](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/defs/test_common.py#L159-L179)。同文件的 `test_build_project`（[test_common.py:37-156](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/defs/test_common.py#L37-L156)）负责先用 `cmake -DBUILD_UNIT_TESTS=ON && make` 编译出 `unitTest` 及各 example 可执行文件，并校验 `--help` 能跑通。

**Golden reference 样本：eagleAcceptTests**。这是投机解码 EAGLE 的「接受」kernel 测试（承接 u7-l1）。它定义一个 fixture `EagleAcceptTest`，核心是一个通用 runner `runEagleAcceptTest`：准备 GPU 张量 → 拷输入到 GPU → 调被测 kernel `kernel::eagleAccept` → 同时跑 CPU 参考实现 `eagleAcceptRef` → 逐元素比对：

`EagleAcceptTest` fixture 与通用 runner，涵盖 GPU 内存管理、kernel 执行、与参考实现比对——见 [unittests/eagleAcceptTests.cpp:34-171](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/unittests/eagleAcceptTests.cpp#L34-L171)。其中调 CPU 参考实现 `eagleAcceptRef` 产出标尺结果（[L104-L105](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/unittests/eagleAcceptTests.cpp#L104-L105)），随后逐 batch、逐位置用 `EXPECT_EQ(hostAcceptedTokenIds[idx], refResult.acceptedTokenIds[...])` 比对（[L120-L146](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/unittests/eagleAcceptTests.cpp#L120-L146)）。`DeviceValidation` 用例（[L714-L739](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/unittests/eagleAcceptTests.cpp#L714-L739)）验证 kernel 会拒绝 CPU 张量（`EXPECT_THROW`），是典型的输入校验测试。

**纯逻辑断言样本：engineExecutorTest**。承接 u5-l3，这个文件测试 `EngineExecutor::BindingSnapshot` 的 `operator==`，完全不需要真实 TensorRT engine，只构造两个 `BindingSnapshot` 比较地址表与形状是否相等：

`BindingSnapshot` 一组六个用例，覆盖「空相等 / 内容相等 / 地址不同 / 维数不同 / 大小不同 / 形状值不同」——见 [unittests/engineExecutorTest.cpp:30-125](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/unittests/engineExecutorTest.cpp#L30-L125)。文件头注释（[L24-L28](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/unittests/engineExecutorTest.cpp#L24-L28)）说明这是从重构前的 `runnerTest.cpp` 迁移来的、专门测 `operator==` 的覆盖，承接 Runner→EngineExecutor 的改名。这种「不依赖重资源、只测纯逻辑」的用例正是单元测试金字塔最底层、跑得最快的部分。

#### 4.3.4 代码实践

**实践目标**：理解如何只跑 `unitTest` 里的某一类用例，以及新增一个 C++ 单测该放哪。

**操作步骤**：

1. 确认 `unitTest` 已被构建（需要 `cmake -DBUILD_UNIT_TESTS=ON`，见 [CMakeLists.txt:162](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L162)）。若无 GPU/未构建，跳到步骤 4 做源码阅读型实践。
2. 列出所有已注册的用例名（**待本地验证**）：
   ```bash
   cd build && ./unitTest --gtest_list_tests | head -40
   ```
3. 只跑 `EagleAcceptTest` 这一类（**待本地验证**）：
   ```bash
   ./unitTest --gtest_filter=EagleAcceptTest.*
   ```
4. （源码阅读型）打开 [unittests/engineExecutorTest.cpp:30-51](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/unittests/engineExecutorTest.cpp#L30-L51)，阅读 `BindingSnapshotEmptyEqual` 与 `BindingSnapshotEquality` 两个用例，确认它们只构造 `BindingSnapshot` 结构、填 `bindings` map、用 `EXPECT_TRUE(s1 == s2)` 断言，全程不碰 GPU 或 engine。
5. 若要新增一个 C++ 单测：在 `unittests/` 下新建 `myFeatureTest.cpp`，写 `#include <gtest/gtest.h>` 和若干 `TEST(...)`，**无需**改任何 CMake（因为 `GLOB_RECURSE` 会自动收走），重新 `make` 即可。

**需要观察的现象**：`--gtest_list_tests` 输出按 fixture 分组的用例树；`--gtest_filter` 只跑匹配的子集，远快于跑全部。

**预期结果**：你能用一条 `--gtest_filter` 命令把单测粒度从「全部五十多个文件」缩到「某一个 fixture」甚至「某一个用例」（如 `--gtest_filter=EngineExecutorTest.BindingSnapshotEmptyEqual`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `unitTest` 链接的是 `gtest_main` 而不是手写 `main()`？删掉 `gtest_main` 会怎样？

**参考答案**：`gtest_main` 提供一个默认 `main()`，它只做 `::testing::InitGoogleTest(...); return RUN_ALL_TESTS();`（见 [CMakeLists.txt:201](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/CMakeLists.txt#L201)）。这样每个测试文件只写 `TEST(...)` 即可，无需各自维护 `main`。删掉 `gtest_main` 后，链接器会因找不到 `main` 符号而报「undefined reference to main」——除非某个源文件手写了 `main`（本项目的测试文件都没有）。

**练习 2**：`eagleAcceptTests.cpp` 为什么要同时跑 `kernel::eagleAccept`（GPU）和 `eagleAcceptRef`（CPU 参考）？只跑 GPU 那半行不行？

**参考答案**：单跑 GPU kernel 只能证明「不崩溃、不超时」，无法证明「结果正确」。`eagleAcceptRef` 是一份朴素但绝对正确的 CPU 实现，作为黄金标尺：把 GPU 输出与它逐元素比对（[eagleAcceptTests.cpp:120-146](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/unittests/eagleAcceptTests.cpp#L120-L146)），才能断言 kernel 的接受长度、接受 token、logits 索引都对。这是 CUDA kernel 测试的标准范式：**被测实现 vs 可信参考**。

## 5. 综合实践

**任务**：你刚为本项目接入了一个新的 decoder-only 模型（参照 u9-l4 的步骤改了 Python 导出前端）。请为这条 PR 设计「最低验证证据」，并解释每一步对应本讲的哪一层测试。

**完整步骤**：

1. **先跑 C++ 单测（第 1 层）**。你的改动若涉及运行时数据结构或 kernel，应先确保 `build/unitTest` 全绿：
   ```bash
   cmake .. -DBUILD_UNIT_TESTS=ON && make -j
   cd build && ./unitTest
   ```
   在 YAML 清单层面，这对应 `l0_pipeline_a30.yml` 的第一条 `test_common.py::test_build_project` 与第二条 `test_unit_tests`（见 [l0_pipeline_a30.yml:2-3](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/test_lists/l0_pipeline_a30.yml#L2-L3)）。

2. **跑 export（三段式第 1 段）**。把新模型导出成 ONNX。若该模型尚未进 YAML，用 `--test-param` 旁路单跑一个配置（绕过 YAML，见 [conftest.py:430-434](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/conftest.py#L430-L434)）：
   ```bash
   pytest --test-param='MyModel-fp16' tests/defs/test_checkpoint_export.py -v
   ```
   这验证 Python 导出前端（u2 系列）没把新模型导坏。

3. **跑 build（三段式第 2 段）**。把 ONNX 编成 engine，对应 `test_engine_build[MyModel-fp16-mxsl4096-mxbs4-mxil2048]`。这验证 C++ 构建器（u4 系列）能解析你导出的 ONNX。

4. **跑 inference（三段式第 3 段）**。加载 engine 做推理，对应 `test_inference[MyModel-fp16-mxsl4096-mxbs4-mxil2048-llm_basic]`。这验证 C++ 运行时（u5 系列）能跑出文本。

5. **把新模型登记进 YAML 清单**。在合适的清单（如 `l0_pipeline_a30.yml`）里加一行 `tests/defs/test_llm_pipeline.py::test_engine_build[MyModel-fp16-mxsl4096-mxbs4-mxil2048]` 与配对的 `test_inference[...]`，然后确认：
   ```bash
   pytest --priority=l0_pipeline_a30 --collect-only -q | grep MyModel
   ```
   能看到这两个用例被选中（见 4.1 的「清单即白名单」机制）。

**最低验证证据的总结**：一条模型改动 PR 至少应附带——①相关 C++ 单测全绿（第 1 层）；②新模型的 export→build→inference 三段在某个 `l0` 清单对应设备上端到端跑通（第 3 层），并在 PR 描述里贴出三段命令与输出目录；③若改了 Python 组件（如插件），补跑 `l0_python_ut.yml` 里相关的 Python 组件测试（第 2 层）。

**为什么必须三段全跑**：因为三段式流水线每一段的产物（ONNX、engine、推理输出）都依赖上一段，且 engine 不可跨设备迁移（u4-l1）。只跑 export 通过，不代表 build 能解析；只跑 build 通过，不代表 inference 数值正确。只有三段端到端全绿，才能证明「从检查点到生成文本」整条链没断。

## 6. 本讲小结

- TensorRT Edge-LLM 的测试是**三层金字塔**：C++ GTest 单测（`unittests/`→`unitTest`）、Python 组件/插件测试（`tests/python-unittests/`）、YAML 驱动的端到端流水线测试（`tests/defs/`）。
- `tests/conftest.py` 是 Python 侧测试的中央调度器：用 `--priority` 把清单名映射到 `tests/test_lists/<name>.yml`，再用 `pytest_generate_tests` 与 `pytest_collection_modifyitems` 两个钩子实现「清单即白名单 + 按 YAML 顺序执行」。
- `tests/test_lists/*.yml` 按**目标 GPU/设备**切分用例（A30/Orin/Thor/Spark 等），因为 engine 不可跨设备迁移、各设备精度支持不同；`l0`/`l1` 前缀区分门槛级与扩展级。
- C++ 单测由顶层 `CMakeLists.txt` 的 `BUILD_UNIT_TESTS=ON` 触发，用 `GLOB_RECURSE` 把 `unittests/` 下所有 `.cpp/.cu` 链接成单一 `unitTest`（链 `gtest_main` 提供默认 `main`），用 `--gtest_filter` 单跑某一类。
- CUDA kernel 单测遵循「**被测实现 vs 可信 CPU 参考**」范式（如 `eagleAcceptTests`），纯逻辑单测则不依赖 GPU/engine（如 `engineExecutorTest` 的 `BindingSnapshot`）。
- 模型改动的最低验证证据是 **export→build→inference 三段式端到端跑通** + 相关 C++ 单测全绿；这正是三层测试共同保障的。

## 7. 下一步学习建议

- 本讲是 u9「特性与扩展」单元的最后一篇，也是整本学习手册的收尾。建议回头把 u9-l4「接入一个新模型架构」与本讲的「综合实践」对照阅读，把「怎么改」与「怎么验证」连成一个完整闭环。
- 若你想深入某个被测组件，可按单测文件名反查对应讲义：例如 `hybridCacheManagerTests.cpp` 对应 u5-l5、`eagleAcceptTests.cpp` 对应 u7-l1、`engineExecutorTest.cpp` 对应 u5-l3、`samplingTests.cpp` 对应 u5-l7。
- 想参与贡献时，先读 [tests/README.md:219-237](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tests/README.md#L219-L237) 的「Adding New Tests」三步法：往清单加一行 → 放好模型文件 → `pytest --priority=... -k "MyModel"` 本地验证。
