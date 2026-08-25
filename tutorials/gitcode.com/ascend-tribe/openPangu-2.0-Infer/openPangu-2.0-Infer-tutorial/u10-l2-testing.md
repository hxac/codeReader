# u10-l2 omni-npu 测试体系与本地跑测

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 omni-npu 测试目录的组织规则：测试树镜像源码树、`test_<模块名>.py` 命名、`unit/` 与 `integration/` 双层分层。
2. 区分两类测试的运行前提：unit 测试不需要 NPU **硬件**（用 mock 验证逻辑与 API 契约），integration 测试需要真机，且无 NPU 时会自动跳过。
3. 掌握 `pytest.ini`、`run_tests.sh` 的用法，能解释 `--` 透传、`--durations-out`、覆盖率配置等细节。
4. 读懂两个 `conftest.py` 为 UT 环境筑起的「两道防线」（entry point 过滤、自定义算子重复注册保护），并理解它们为什么必要——这正是 u2-l1 讲过的 entry points 机制在测试场景的反向应用。
5. 仿照现有用例，为自己新写的代码（例如 u2-l4 的 patch、u5-l1 的配置加载）补一个能通过的最小单测。

## 2. 前置知识

### 2.1 pytest 的四个基础概念

- **收集（collect）**：pytest 按 `test_*.py` 文件、`Test*` 类、`test_*` 函数三层规则扫描出所有用例；`--collect-only` 只列名单不执行。
- **fixture**：以 `@pytest.fixture` 装饰的函数，为用例提供可复用的前置资源（对象、环境），用例以参数名注入的方式使用它。
- **marker（标记）**：以 `@pytest.mark.xxx` 给用例打标签，供 `-m xxx` 筛选；`--strict-markers` 要求所有标签必须事先在配置里注册，拼错直接报错。
- **conftest.py**：pytest 约定的「局部配置文件」，无需 import 即自动生效；子目录的 conftest 只对该子树生效，且在收集阶段早期执行——因此常被用来在**任何测试模块 import 之前**改造环境。

### 2.2 mock：不碰硬件测逻辑

NPU 代码的单测核心矛盾是：被测对象（如 `NPUCommunicator`）运行时依赖 `torch.npu`、HCCL 进程组，而测试机没有这些。解法是 **mock（打桩）**：用 `unittest.mock.patch` 把真实依赖替换成假对象，只验证「被测代码调用了谁、传了什么参数、返回值如何流转」这类**逻辑契约**。本仓库单测大量使用 `Mock`/`MagicMock`/`patch`，这是阅读样例前必须熟悉的工具。

### 2.3 「不需要 NPU 硬件」≠「不需要 torch_npu 软件包」

一个容易踩的坑：很多源码模块在文件顶部就 `import torch_npu`、`import vllm`（例如 [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:L14-L19](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L14-L19)）。所以 unit 测试的准确前提是：**环境里装着 torch、torch_npu、vllm 这些软件包，但没有真实 NPU 设备、不做分布式初始化**。最自然的运行场所是 u1-l4 部署出的推理容器，或 u10-l1 构建的 omniinfer 镜像容器。

### 2.4 与前面讲义的衔接

- u2-l1 讲过 omni-npu 通过 pyproject 的 entry points 被 vLLM 发现；本讲会看到 `tests/conftest.py` 如何在 UT 里**反向利用**这一机制——把 `omni.*` 入口组全部过滤掉。
- u2-l4 讲过 PatchManager 与 patch 目录映射；本讲会把 `tests/unit/vllm_patch/test_patch_dir_mapping.py` 作为「给纯函数补测试」的范例，综合实践也会给配置加载代码补测试（呼应 u5-l1）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [components/omni-npu/pytest.ini](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pytest.ini#L1-L35) | pytest 权威配置：发现规则、addopts、marker 注册 |
| [components/omni-npu/pyproject.toml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pyproject.toml#L25-L28) | `test` 依赖组（pytest、pytest-cov）；另有一份重复的 pytest 配置 |
| [components/omni-npu/tests/run_tests.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/run_tests.sh#L1-L145) | 一键跑测脚本：unit/integration/all 三分支 + 覆盖率 + 依赖自装 |
| [components/omni-npu/tests/conftest.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/conftest.py#L1-L30) | 顶层 conftest：过滤 `omni.*` entry points + `default_vllm_config` fixture |
| [components/omni-npu/tests/unit/conftest.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/conftest.py#L1-L29) | unit 子树 conftest：自定义算子重复注册保护 |
| [components/omni-npu/tests/integration/utils/common_utils.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/integration/utils/common_utils.py#L1-L13) | `NPU_AVAILABLE` 探测与 `skipif_no_npu` 跳过装饰器 |
| [components/omni-npu/tests/unit/distributed/test_communicator.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/distributed/test_communicator.py#L1-L150) | 文档点名的「模板用例」：mock 风格测 NPUCommunicator |
| [components/omni-npu/tests/unit/connector/test_register.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/connector/test_register.py#L1-L120) | 「路径常量 + patch」风格的另一范例 |
| [components/omni-npu/tests/STRUCTURE.md](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/STRUCTURE.md#L59-L87) | 新增测试的目录与命名规约 |
| [components/omni-npu/tests/README.md](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/README.md#L125-L156) | 多容器并行 UT 工作流文档 |
| [components/omni-npu/tests/ut_config.sh](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/ut_config.sh#L20-L39) | 多容器 UT 的容器↔NPU 卡映射与分组参数 |
| [components/omni-npu/tests/.coveragerc](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/.coveragerc#L1-L14) | 真正生效的覆盖率配置（排除 vllm_patches 等） |

## 4. 核心概念与源码讲解

### 4.1 pytest 组织：目录镜像、命名约定与双配置文件

#### 4.1.1 概念说明

omni-npu 的测试采用「**测试树镜像源码树**」的组织法：`src/omni_npu/` 下每个子包，在 `tests/unit/` 与 `tests/integration/` 下各有一个同名子目录，测试文件名是 `test_<源文件名>.py`。好处是定位零成本——改 `src/omni_npu/distributed/communicator.py`，就知道测试在 `tests/*/distributed/test_communicator.py`。

这个组件同时存在**两份 pytest 配置**：根目录 `pytest.ini` 与 `pyproject.toml` 里的 `[tool.pytest.ini_options]`。pytest 的配置优先级是 `pytest.ini` > `pyproject.toml` > `tox.ini` > `setup.cfg`，所以 **pytest.ini 是唯一生效的权威配置**，pyproject 里那份只是残留（两者 addopts 并不一致，前者多了 `--strict-markers` 和 `--disable-warnings`）。

#### 4.1.2 核心流程

一次 `pytest unit/`（在 `tests/` 目录下执行）的执行流程：

1. **定 rootdir**：从参数公共祖先向上找配置文件，命中组件根的 `pytest.ini` → rootdir 为 `components/omni-npu`，marker 注册与 addopts 生效。
2. **加载 conftest**：先执行 `tests/conftest.py`（过滤 entry points），再执行 `tests/unit/conftest.py`（算子注册保护）。
3. **收集**：按 `test_*.py` / `Test*` / `test_*` 三层规则扫出用例，`diagnostics/` 等目录因不匹配规则被跳过或按路径决定是否纳入。
4. **执行**：逐用例运行，mock 型用例全程不触碰 NPU 设备。
5. **覆盖率**（`run_tests.sh` 自动附加）：`--cov=omni_npu --cov-config=./.coveragerc` 统计并输出 `term-missing` 与 `htmlcov/` 报告。

#### 4.1.3 源码精读

**权威配置 pytest.ini**。发现规则与输出选项：

- [components/omni-npu/pytest.ini:L5-L8](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pytest.ini#L5-L8)：`testpaths = tests`（不带参数启动 pytest 时默认收集 `tests/`），加上文件/类/函数三层命名规则。
- [components/omni-npu/pytest.ini:L11-L15](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pytest.ini#L11-L15)：默认 `-v --tb=short --strict-markers --disable-warnings`。注意 `--strict-markers`：用了未注册的 marker 会直接报错，这就是下一条必须存在的原因。
- [components/omni-npu/pytest.ini:L18-L22](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pytest.ini#L18-L22)：注册了 `unit`、`integration`、`slow`、`multi_device` 四个 marker。**现实是 marker 只被部分使用**（integration 的 communicator 用例打了标记，很多 unit 用例没打），所以 `-m unit` 筛选并不完备，**按目录筛选（`pytest unit/`）才是可靠口径**。
- [components/omni-npu/pytest.ini:L24-L35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pytest.ini#L24-L35)：文件尾部的 `[coverage:run]`/`[coverage:report]` 段是**死配置**——coverage.py 只读 `.coveragerc`、`setup.cfg`、`tox.ini`、`pyproject.toml`，不读 pytest.ini；真正生效的覆盖率配置是 `run_tests.sh` 显式传入的 `tests/.coveragerc`。

**pyproject 的 test 依赖组**：[components/omni-npu/pyproject.toml:L25-L28](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pyproject.toml#L25-L28) 声明 `pip install -e ".[test]"` 会装 `pytest>=7.0` 与 `pytest-cov>=4.0`；而 `pytest-split`（分组跑测用）没在这里，由 `run_tests.sh` 运行时自动补装。重复的那份配置在 [components/omni-npu/pyproject.toml:L47-L52](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pyproject.toml#L47-L52)。

**新增测试的规约**（STRUCTURE.md）：为 `src/omni_npu/foo/bar.py` 加测试，就建 `tests/unit/foo/test_bar.py`（必要时补 `__init__.py`）：

- [components/omni-npu/tests/STRUCTURE.md:L63-L79](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/STRUCTURE.md#L63-L79)：给出 mkdir/touch 的三行操作模板，integration 侧同理。
- [components/omni-npu/tests/STRUCTURE.md:L81-L87](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/STRUCTURE.md#L81-L87)：命名模式固定为 `test_<source_filename>.py`。

**重要提醒：文档滞后于目录树。** README/QUICKSTART/STRUCTURE 都写着「目前只实现了 NPUCommunicator 测试」（见 [components/omni-npu/tests/QUICKSTART.md:L5-L6](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/QUICKSTART.md#L5-L6) 的 Current Status 一行），但真实目录树里 `tests/unit/` 已有约 80 个测试文件，覆盖 `attention`、`compilation`、`config`、`connector`、`distributed`、`layers`、`lopt`、`models`、`parsers`、`platform`、`quantization`、`sample`、`vllm_patch(es)` 等十几个子包。**以目录树为准，不要以文档的「To Be Implemented」清单为准**。另外注意两个容易迷惑的细节：

- `tests/unit/vllm_patch/`（单数）与 `tests/unit/vllm_patches/`（复数）**两个目录并存**，前者放 patch 目录映射类测试，后者放具体 patch 行为测试。
- `tests/diagnostics/` 挂在 `tests/` 根下而不在 `unit/` 里，因此 `./run_tests.sh unit` 收集不到它，只有 `all`（不带参数、走 `testpaths=tests`）才会带上。

#### 4.1.4 代码实践

**实践目标**：验证「pytest.ini 是生效配置」与「收集范围由路径决定」，并对测试规模建立直观感受。

**操作步骤**（在装有 vllm/torch_npu 的容器内，进入 `components/omni-npu/tests/` 目录）：

1. `pytest unit/ --collect-only -q 2>&1 | head -5`，观察输出头部的 `rootdir: ...` 与 `configfile: pytest.ini` 两行。
2. `pytest unit/ --collect-only -q 2>&1 | tail -3`，看最后的用例计数。
3. `pytest --collect-only -q 2>&1 | grep -c diagnostics`，对比不带路径（走 `testpaths=tests`）时 `diagnostics/` 是否被纳入。
4. `pytest unit/ -m unit --collect-only -q 2>&1 | tail -3`，对比 marker 筛选后的数目与第 2 步的数目差异。

**需要观察的现象**：第 1 步 rootdir 指向组件根且 configfile 为 `pytest.ini`；第 4 步选出的用例数明显少于第 2 步（很多 unit 用例没打 marker）。

**预期结果**：确认「目录筛选是全集、marker 筛选是子集」，今后跑测一律用路径参数。具体计数与输出——待本地验证（编写本讲义的环境未安装 torch_npu/vllm，无法代跑）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tests/diagnostics/` 下的用例在 `./run_tests.sh unit` 时永远不会执行？

**答案**：`run_tests.sh` 的 unit 分支写死了路径参数 `pytest unit/ ...`（见 run_tests.sh L95），收集范围被限制在 `tests/unit/` 子树；`diagnostics/` 位于 `tests/` 根下，只有不带路径参数的 `all` 分支才会经 `testpaths = tests` 把它收进来。

**练习 2**：如果你在测试类上写了 `@pytest.mark.unitt`（拼错），会发生什么？为什么？

**答案**：pytest 直接报错退出。因为 pytest.ini 的 addopts 带有 `--strict-markers`，而 `unitt` 不在 L18-L22 注册的四个 marker 里。这个设计的价值是把「标签拼错导致筛选静默失效」变成显式失败。

**练习 3**：`pytest.ini` 里的 `[coverage:run]` 段为什么不起作用？

**答案**：coverage.py 只识别 `.coveragerc`、`setup.cfg`、`tox.ini`、`pyproject.toml` 这几种配置载体，不解析 pytest.ini；实际生效的是 run_tests.sh 显式传给 `--cov-config` 的 `tests/.coveragerc`。

### 4.2 conftest.py：UT 环境的两道防线

#### 4.2.1 概念说明

单测要回答的问题是「**只**测 omni-npu 自己的逻辑」。但这个仓库的运行环境里还有别的插件（u1-l1 讲过的 omni-cache 等）也通过 entry points 注册钩子，vLLM 的自定义算子注册也不是幂等的。两个 conftest 分别解决这两个「环境污染」问题：

- `tests/conftest.py`：**防线一**——把 `omni.*` 入口组的 entry points 全部过滤为空，防止 omni-cache 等兄弟插件在 UT 里被 vLLM 发现并改变 omni-npu 的行为。
- `tests/unit/conftest.py`：**防线二**——把 vLLM 的自定义算子注册函数包一层「已注册就跳过」的保护，防止几十个测试模块重复注册同名算子时报错。

#### 4.2.2 核心流程

防线一的生效时机（顺序很关键）：

```text
pytest 启动
  → 加载 tests/conftest.py（此时还没 import 任何 omni_npu 模块）
  → 猴子补丁替换 importlib.metadata.entry_points
  → 收集并 import 各测试模块 → 触发 import omni_npu / vllm
      → vLLM 内部经 entry_points(group="omni.*") 发现插件 → 得到空列表
  → omni-cache 等兄弟插件完全不参与 UT
```

防线二的生效时机：pytest 的 `pytest_configure` 钩子在收集前运行，先于任何测试模块 import vLLM 算子注册工具，因此替换能赶上所有注册调用。

#### 4.2.3 源码精读

**防线一：entry point 过滤**。

- [components/omni-npu/tests/conftest.py:L4-L7](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/conftest.py#L4-L7)：注释写明动机——这些 out-of-tree 插件会在测试环境里以不可预期的方式改变行为或 mock 返回值，所以 UT 不启用它们。这正是 u2-l1 讲的发现机制：**注册靠 entry points，隔离也靠拦截 entry points**。
- [components/omni-npu/tests/conftest.py:L10-L20](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/conftest.py#L10-L20)：实现是标准猴子补丁——保存原函数 `_orig_entry_points`，替换为 `_filtered_entry_points`：只要查询的 group 名以 `omni.` 开头就返回空列表，其余查询原样放行。注意它过滤的是 `omni.*` 组（如 `omni.kv_connectors`），**不动** `vllm.platform_plugins` 等组，所以 omni-npu 自己作为平台插件的注册不受影响（UT 里 vLLM 也不走那条发现路径）。
- [components/omni-npu/tests/conftest.py:L25-L30](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/conftest.py#L25-L30)：顺手提供的 `default_vllm_config` fixture——构造一个默认 `VllmConfig` 并挂进 `set_current_vllm_config` 上下文。很多被测代码（层、采样器）运行时会读「当前 vllm 配置」这个全局态，无它则单测直接崩；4.3 节会看到用例以参数名注入使用它。

**防线二：算子重复注册保护**。

- [components/omni-npu/tests/unit/conftest.py:L17-L29](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/conftest.py#L17-L29)：在 `pytest_configure` 钩子里把 `vllm.utils.torch_utils.direct_register_custom_op` 替换为 `safe_direct_register_custom_op`：先用 `_is_custom_op_registered`（L8-L14，检查 `torch.ops.<namespace>.<op>` 是否已存在）判断，已注册直接返回 None，否则调用原函数。u3-l2 讲过 omni-npu 大量使用 `torch.ops.custom`/`torch.ops.vllm` 自注册算子；每个测试文件 import 时都会触发注册，没有这层保护，第二个测试模块加载就会因重名注册而失败。整个替换包在 `try/except ImportError` 里，环境里没有 vllm 时静默跳过。

#### 4.2.4 代码实践

**实践目标**：搞清「防线一必须在 conftest、且必须在 import pytest/测试模块之前执行」这一位置约束。

**操作步骤**（源码阅读型实践，不改源码）：

1. 读 [components/omni-npu/tests/conftest.py:L8-L22](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/conftest.py#L8-L22)，注意补丁赋值语句在 `import pytest` **之前**。
2. 回答：如果这段过滤代码搬进某个测试文件（而非 conftest），还能拦住 vLLM 发现 omni-cache 吗？
3. 在容器里手工验证过滤效果：`python3 -c "import importlib.metadata as m; print([e.name for e in m.entry_points() if e.group.startswith('omni.')])"`，记下输出；再 `cd tests && pytest unit/vllm_patch --collect-only -q`，确认收集正常（说明 conftest 在起作用、未误伤）。

**需要观察的现象**：第 2 步应得出「不能」——测试文件被 import 时 vLLM/omni_npu 早已被前面的模块加载，发现时机已过。

**预期结果**：理解 conftest 的执行时机是「收集前、一切被测模块 import 前」，这是它区别于普通工具模块的本质。第 3 步的具体输出取决于容器内安装了哪些 omni-* 包——待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：防线一为什么按「group 以 `omni.` 开头」过滤，而不是把 `vllm.platform_plugins` 也一起过滤掉？

**答案**：过滤的目标是 omni-npu 的**兄弟插件**（omni-cache 等 `omni.*` 入口组），它们会注入额外行为污染被测逻辑；而 `vllm.platform_plugins` 等 vLLM 自身的发现机制不是污染源，UT 里 omni-npu 的行为本来就靠直接 import 来测，不需要也不应该破坏 vLLM 自身的注册表结构。

**练习 2**：`default_vllm_config` 是 function 级 fixture（默认 scope）。一个测试类里有 8 个用例都注入它，`VllmConfig()` 会被构造几次？这有什么代价与好处？

**答案**：构造 8 次，每个用例各得一个干净上下文。代价是重复构造的开销；好处是用例间全局态完全隔离——前一个用例对全局配置的污染不会泄漏到下一个，这正是单测「可重复、可独立运行」的要求。

**练习 3**：防线二的 `_is_custom_op_registered` 用 `hasattr(torch.ops, namespace)` 探测。为什么不直接 try/except 注册一次来判重？

**答案**：`direct_register_custom_op` 重复注册同名算子在 torch 侧会抛错或产生不可预期行为（该函数本身无幂等保证）；在调用前用 hasattr 无副作用地探测，是最安全的前置判断。同时保留原函数引用，未注册时行为与 vLLM 完全一致，不改变注册语义。

### 4.3 测试分层：unit 与 integration 的边界及跳过机制

#### 4.3.1 概念说明

同一份被测代码（如 communicator）有两套测试，分工不同：

| 维度 | unit | integration |
| --- | --- | --- |
| 硬件 | 不需要 NPU 设备 | 需要真机（多卡用例还需 ≥2 卡与 torchrun） |
| 手段 | mock 依赖，验证逻辑与 API 契约 | 真实设备上端到端验证 |
| 数量/速度 | 多、快，可随时跑 | 少、慢，需预约真机 |
| 无 NPU 时的行为 | 正常执行 | 通过 skipif 自动跳过，不报失败 |

关键机制是**探测 + 条件跳过**：import 时探测 `torch_npu` 与设备数，得到 `NPU_AVAILABLE` 布尔量，再做成 `skipif_no_npu` 装饰器套在集成测试类上——于是一套测试树在开发机与真机上都可直接运行，无需手工挑选。

#### 4.3.2 核心流程

集成测试在一台无 NPU 机器上的「跳过链」：

```text
import common_utils
  → try import torch_npu
      → 失败：NPU_AVAILABLE = False
  → skipif_no_npu = unittest.skipIf(not NPU_AVAILABLE, "NPU hardware not available")
收集阶段发现类上挂了 skipIf(条件=True)
  → 该类所有用例标记 SKIPPED，逐条输出 "SKIPPED (NPU hardware not available)"
进程退出码仍为 0（跳过不算失败）
```

多卡用例则更进一步：单进程 pytest 排除之，由 `torchrun --nproc_per_node=2` 拉起两个进程各跑一份。

#### 4.3.3 源码精读

**探测与跳过装饰器**：[components/omni-npu/tests/integration/utils/common_utils.py:L5-L13](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/integration/utils/common_utils.py#L5-L13) —— `NPU_AVAILABLE = hasattr(torch, 'npu') and torch.npu.device_count() > 0`（两层判断：包在 + 设备在），失败统一回退 False；`skipif_no_npu` 只是 `unittest.skipIf` 的一行封装。

**集成侧的用法**：[components/omni-npu/tests/integration/distributed/test_communicator.py:L23-L31](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/integration/distributed/test_communicator.py#L23-L31) —— 类上先 `@pytest.mark.integration` 再 `@skipif_no_npu`，`setUpClass` 里还兜底抛 `SkipTest`（防装饰器被误删）。用例体里是 `torch.device('npu:0')`、真实张量与真实进程组，与 unit 侧的 mock 形成鲜明对照。

**unit 侧的模板用例（mock 三件套）**：[components/omni-npu/tests/unit/distributed/test_communicator.py:L16-L44](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/distributed/test_communicator.py#L16-L44)：

- L16-L22 定义 `mock_distributed_environment` 上下文管理器，一次性 patch 掉 `torch.distributed.get_rank/get_world_size` 与父类 `CudaCommunicator.__init__`——**测子类时不让父类构造函数跑真分布式**，这是继承场景 mock 的标准姿势。
- L25-L26 类上打 `@pytest.mark.unit`。
- L28-L44 第一个用例：`Mock(spec=ProcessGroup)` 造假进程组，构造后断言 `communicator.dist_module is torch.distributed`（契约：NPU 侧通信最终转调 torch.distributed，即 HCCL 后端——呼应 u2-l2）。

再看一个典型断言「委托关系」的用例：[components/omni-npu/tests/unit/distributed/test_communicator.py:L73-L90](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/distributed/test_communicator.py#L73-L90) —— patch 掉 `torch.distributed.all_reduce` 后调用 `communicator.all_reduce(t)`，断言 mock 恰被调用一次、参数为 `(t, group=device_group)`、返回的就是原张量。单测在无硬件时能验证的正是这种「调了谁、怎么传、返回谁」。

**路径常量风格**：[components/omni-npu/tests/unit/connector/test_register.py:L10-L12](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/connector/test_register.py#L10-L12) 把所有 patch 目标路径提取为模块级常量（`KV_CONNECTOR_FACTORY_PATH` 等），L28-L45 的用例用 fixture 提供假 logger/假工厂，验证 `_safe_register` 的注册与去重分支。写新测试时建议沿用这一风格：patch 目标集中声明，用例短小聚焦。

**给纯函数补测试的范例**：[components/omni-npu/tests/unit/vllm_patch/test_patch_dir_mapping.py:L7-L23](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/vllm_patch/test_patch_dir_mapping.py#L7-L23) 直接断言 [components/omni-npu/src/omni_npu/vllm_patches/__init__.py:L111-L122](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/__init__.py#L111-L122) 中 `_get_patch_dir_names` 的映射表：`openpangu_v2` 展开为 `pangu_v2_base` + `pangu_sink_swa_mla` 两个目录、minimax_m2 只映射到 `minimax`。纯函数测试不需要任何 mock，是上手补测试的最佳起点。

**一个诚实的例外——`unit/` 下也有要真机的用例**：[components/omni-npu/tests/unit/layers/st/test_activation.py:L11-L27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/layers/st/test_activation.py#L11-L27) 的 fixture 直接 `torch.device(f"npu:{FIRST_DEVICE}")` 并在 NPU 上建张量，比较 `NPUSiluAndMul` 与 SwiGLU 参考实现（CPU 语义）的数值一致性。`layers/st/` 子目录整层如此。所以准确的说法是：**unit/ 绝大多数用例免硬件，但 st/（单测里的算子数值验证）例外**；在纯 CPU 机器上跑 `./run_tests.sh unit` 会在这些用例上失败而非跳过（它们没挂 skipif）。这也是 CI 干脆把整个 UT 放进带 NPU 的容器里跑的原因（见 4.4）。

#### 4.3.4 代码实践

**实践目标**：亲眼看一次「自动跳过」，并确认多卡用例与单卡用例的分轨执行。

**操作步骤**：

1. 在**无 NPU** 的开发机（装好 torch_npu/vllm 软件包）上：`cd components/omni-npu/tests && pytest integration/ -v 2>&1 | tail -20`。
2. 观察每条用例的状态列是 `SKIPPED` 及括号里的原因字符串。
3. 对照 [components/omni-npu/tests/run_tests.sh:L106-L113](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/run_tests.sh#L106-L113)：integration 分支先用 `-k "not TestNPUCommunicatorMultiDevice"` 排除多卡类，再用 `torchrun --nproc_per_node=2 -m pytest` 单独跑它。
4. 在有 NPU 的容器里执行 `./run_tests.sh integration`，对比第 1 步的输出。

**需要观察的现象**：无 NPU 时 integration 全部 SKIPPED 且退出码为 0；有 NPU 时同样的命令真实执行，多卡类出现两份（每进程一份）报告。

**预期结果**：建立「同一测试树、两种环境、行为自适应」的直观认识。两步输出——待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`NPU_AVAILABLE` 的判断为什么是 `hasattr(torch, 'npu') and torch.npu.device_count() > 0` 两个条件的与？

**答案**：`import torch_npu` 成功只说明软件包在（torch 上挂了 npu 命名空间），不代表机器上有设备；驱动缺失或容器没透传设备时 `device_count()` 为 0。两个条件分别覆盖「包在不在」与「卡在不在」，任一不满足都无法跑真机用例。

**练习 2**：集成测试类同时挂了 `@pytest.mark.integration`、`@skipif_no_npu`，又在 `setUpClass` 里抛 `SkipTest`。三重保险各防什么？

**答案**：marker 供 `-m` 筛选与统计口径；skipif 是主跳过机制（类级、收集阶段生效）；setUpClass 兜底防「装饰器在重构中被误删」——即使删了装饰器，类初始化时也会跳过而不是在真机用例里崩溃。这是防御性测试代码的典型写法。

**练习 3**：为什么 `unit/layers/st/test_activation.py` 这类需要真机的用例被放在 `unit/` 而不是 `integration/`？

**答案**：从验证目标看，它测的是单个算子（NPUSiluAndMul）相对数学参考实现的正确性，粒度是「单元」（层/算子级数值一致性），不是跨模块的端到端行为；分层依据是**被测对象的粒度**，而「要不要硬件」只是伴随特征。这也提醒我们：目录名是强信号但非绝对承诺，读用例的 import 与 fixture 才是最终判据。

### 4.4 镜像内跑测：run_tests.sh 与多容器并行 CI

#### 4.4.1 概念说明

`run_tests.sh` 是跑测的统一入口，职责包括：解析测试类型与透传参数、自动补装 pytest/pytest-split/pytest-cov、打印 git 状态快照（可追溯性）、按分支拼装 pytest 命令并默认开覆盖率。它也是**容器内外的工作分界**：宿主机负责编排（起容器、同步代码、收工件），容器内执行的就是这个脚本。

多容器并行 CI 解决的是另一个问题：全套 UT（含 st/ 这类要真机的用例）需要 NPU，而一台 16 卡主机一次只给一个任务跑太浪费。方案是把主机切成 4 个容器、每个分 4 张卡，用 pytest-split 按**历史耗时**把用例均衡地分成 4 组并行跑，最后合并覆盖率与耗时数据。

#### 4.4.2 核心流程

`run_tests.sh` 的参数流：

```text
./run_tests.sh unit -- -x -k communicator
        │      │      │
        │      │      └── "--" 之后的一切原样透传给 pytest
        │      └──（本例无；还可出现 --durations-out <file>）
        └── 第一个位置参数 ∈ {unit, integration, all} 决定分支
分支 → 拼装 pytest 命令（unit/all 附加 --cov 三件套）→ 执行
```

多容器并行的数据流：

```text
宿主机 run_docker.sh：起 DT_1..DT_4 四个容器（卡 0-3/4-7/8-11/12-15）
宿主机 concurrent_test_run_multi_docker.sh：
    对每个容器 docker exec：
      同步仓库代码进容器 → cd tests && CI_MULTI_DOCKER=1 bash ./run_tests.sh \
          --durations-out <每容器耗时文件> all -- --splits 4 \
          --splitting-algorithm least_duration --group N
    收集：install_logs/、test_durations_from_dockers/、coverage_from_dockers/
后续：ut_CI_check 脚本做日志解析、覆盖率阈值检查、分组均衡检查、增量覆盖率
```

其中 least_duration 分组是贪心算法：按历史耗时 \( d(t) \) 把用例从大到小逐个放入当前负载 \[ \text{load}(g)=sum_{t in g}d(t) \] 最小的组，目标是四组负载尽量相等。

#### 4.4.3 源码精读

**参数解析**：[components/omni-npu/tests/run_tests.sh:L9-L50](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/run_tests.sh#L9-L50) —— 第一个参数若恰为 unit/integration/all 则消费为 TEST_TYPE（默认 all）；随后循环识别 `--`（其后全部进 pytest_args）、`--durations-out`（记录耗时输出文件），其余一律透传。手写 case 而非 getopts，是为了让 `--` 之后能带任何 pytest 参数。

**依赖自装**：[components/omni-npu/tests/run_tests.sh:L52-L68](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/run_tests.sh#L52-L68) —— pytest 缺失则 `pip install -e ".[test]"`；pytest-split、pytest-cov 缺失则单独补装。这就是 QUICKSTART 里「装好 `.test` 后一条命令即可」的原因（[components/omni-npu/tests/QUICKSTART.md:L7-L19](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/QUICKSTART.md#L7-L19) 的 TL;DR 四行）。

**容器钩子与可追溯性**：[components/omni-npu/tests/run_tests.sh:L72-L89](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/run_tests.sh#L72-L89) —— `CI_MULTI_DOCKER=1` 时强制把容器内路径写进 PYTHONPATH（L72-L74，与 ut_config.sh 的 `/workspace/omniinfer/...` 布局配套）；L83-L89 打印 git status/branch/最近 5 条提交，让每份测试日志自带代码版本指纹。

**三个执行分支**：

- unit：[components/omni-npu/tests/run_tests.sh:L91-L105](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/run_tests.sh#L91-L105)，`pytest unit/ --tb=short --cov=omni_npu --cov-report=term-missing --cov-report=html --cov-config=./.coveragerc -v`。
- integration：[components/omni-npu/tests/run_tests.sh:L106-L113](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/run_tests.sh#L106-L113)，见 4.3.3 的分轨说明。
- all：[components/omni-npu/tests/run_tests.sh:L114-L129](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/run_tests.sh#L114-L129)，不带路径参数，靠 `testpaths=tests` 收全集（因此包含 diagnostics/）。

**生效的覆盖率配置**：[components/omni-npu/tests/.coveragerc:L1-L14](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/.coveragerc#L1-L14) —— `parallel = True` + `concurrency = multiprocessing, thread`（多进程/多线程各自落 `.coverage.*` 再合并，服务多容器场景）；`omit` 排除 `vllm_patches/*` 与 `npu_pangu.py`（前者是 u2-l4 的补丁胶水、后者是 u3-l2 的算子薄封装，行覆盖无意义）；`[paths]` 段把容器内路径映射回源码路径，保证跨容器合并时同名归并。

**容器↔卡的映射**：[components/omni-npu/tests/ut_config.sh:L20-L25](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/ut_config.sh#L20-L25) 定义 DT_1..DT_4 分别可见卡 `0,1,2,3` / `4,5,6,7` / `8,9,10,11` / `12,13,14,15`；[components/omni-npu/tests/ut_config.sh:L29-L38](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/ut_config.sh#L29-L38) 定义每个容器的测试参数：统一跑 `all`，经 `--` 透传 `--splits 4 --splitting-algorithm least_duration --group N`。改容器数、卡映射或分组就改这一个文件（[components/omni-npu/tests/README.md:L130-L133](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/README.md#L130-L133) 明确说了这点）。

**起容器**：[components/omni-npu/tests/run_docker.sh:L29-L55](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/run_docker.sh#L29-L55) —— `docker run` 透传 `ASCEND_RT_VISIBLE_DEVICES`（每个容器只见自己的 4 张卡）、`--device=/dev/davinci_manager` 等昇腾设备文件、挂载宿主机驱动目录——与 u1-l4 的 NPU 容器三要素同款；另外设了 `PYTHONHASHSEED=123`（与 u6-l1 讲的 proxy 侧 APC 哈希种子同一个动机：可复现哈希）。

**并发编排**：[components/omni-npu/tests/concurrent_test_run_multi_docker.sh:L26-L45](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/concurrent_test_run_multi_docker.sh#L26-L45) —— 对每个容器 `docker exec`：清空并拷贝仓库进容器、`cd tests`、以 `CI_MULTI_DOCKER=1` 调 run_tests.sh 并指定 `--durations-out`；产物目录（install_logs、test_durations_from_dockers、coverage_from_dockers）的定义见 [components/omni-npu/tests/README.md:L143-L156](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/README.md#L143-L156)。

#### 4.4.4 代码实践

**实践目标**：吃透 `--` 透传机制与 `--durations-out` 的作用，为读懂 CI 分组打下基础。

**操作步骤**（任一装有依赖的环境）：

1. 读 [components/omni-npu/tests/run_tests.sh:L24-L50](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/run_tests.sh#L24-L50)，画出一个三行的参数流向表：`unit` / `--durations-out` / `-x` 各自落到哪个变量。
2. 在容器内试跑一条最小命令：`cd components/omni-npu/tests && ./run_tests.sh unit -- --collect-only -q`，验证 `--` 后的参数确实到了 pytest。
3. 加 `--durations-out /tmp/d.json` 再跑一次，对比 pytest 命令行回显里多出的 `-p ut_CI_check.ut_CI_durations_plugin --durations-out /tmp/d.json`，并查看生成的 json。

**需要观察的现象**：第 2 步只收集不执行；第 3 步命令回显包含 durations 插件参数，`/tmp/d.json` 里是逐用例耗时（供下次 least_duration 分组用）。

**预期结果**：理解「run_tests.sh 不发明新参数，只做分支与拼装，复杂参数一律透传 pytest」。实际输出——待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `run_tests.sh` 要在跑测前打印 `git status`、`git branch` 和最近 5 条提交？

**答案**：测试日志常被保存、上传、事后翻查（尤其多容器 CI 每个容器一份日志）；把代码版本指纹打进日志开头，失败复盘时才能确定当时跑的是哪个 commit、工作区是否干净，避免「日志与代码对不上」的悬案。

**练习 2**：`.coveragerc` 里 `parallel = True` 与 `[paths]` 段分别是为哪个场景服务的？

**答案**：`parallel = True` 让每个进程（含多容器里的每个 pytest 进程）各自写 `.coverage.<主机名>.<pid>` 再统一 `coverage combine`，避免并发写坏同一文件；`[paths]` 把容器内路径（如 `/workspace/omniinfer/components/omni-npu/src`）与常规路径映射到同一逻辑源码位置，合并四个容器的数据时才能正确归并同一文件的覆盖行。

**练习 3**：如果把 `ut_config.sh` 里的 `UT_SPLITS` 从 4 改成 2 但容器仍起 4 个，会发生什么？

**答案**：每个容器的参数都变成 `--splits 2 --group N`，而 N 取值为 1..4；pytest-split 会因 group 编号超出分组数而报错（group 3、4 不存在），即使不报错也会出现两组容器重复跑同一批用例、另两组空转。正确做法是容器数、`UT_SPLITS`、`--group N` 三者同步改——这正是把三者集中在 ut_config.sh 一个文件里维护的原因。

## 5. 综合实践

**任务**：为 u5-l1 讲过的模型指纹匹配逻辑补一个最小单测，并在容器内跑通。目标是走完「选靶 → 仿写 → 放置 → 运行 → 核对」的完整闭环。

**靶子**：`parse_hf_config`（把 HF config 的超参指纹反查成模型规格名）与 `_loader_configs_data`（加载 json 表）。两者都是纯逻辑，不需要 mock，但注意其所在模块顶部 import 了 torch_npu 与 vllm（[components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:L14-L19](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L14-L19)），所以测试必须在推理容器里跑。

**步骤**：

1. **读懂被测语义**：匹配循环在 [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:L253-L276](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L253-L276)：逐条比对表项的全部键值，全中才算命中；0 命中回退用 `model_type` 原名，恰好 1 命中取表项名，多命中抛 `RuntimeError`（deepseek 系特判除外）；返回二元组 `(model_name, quant_type)`（[L312](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L312)），无量化配置时 `quant_type == "bf16"`。
2. **仿照 4.3.3 的纯函数范例**，新建 `tests/unit/config/test_loader_match_hf.py`（`tests/unit/config/` 目录已存在，放着 test_features.py 与 test_reasoning.py）。以下是**示例代码**（非仓库原有文件）：

```python
# 示例代码：tests/unit/config/test_loader_match_hf.py
"""model_config 指纹表与 parse_hf_config 的最小单测（无需 NPU 设备）。"""
import os
from types import SimpleNamespace

import pytest

from omni_npu.model_config.config_loader.loader import (
    _loader_configs_data,
    default_config_path,
    parse_hf_config,
)

MATCH_JSON = os.path.join(default_config_path, "match_hf_configs.json")


@pytest.mark.unit
class TestMatchHfConfigsTable:
    def test_table_is_non_empty_dict(self):
        data = _loader_configs_data(MATCH_JSON)
        assert isinstance(data, dict) and len(data) > 0

    def test_every_entry_carries_model_type(self):
        # model_type 参与指纹比对，缺了它表项永远无法命中
        for name, params in _loader_configs_data(MATCH_JSON).items():
            assert "model_type" in params, f"entry {name} lacks model_type"


@pytest.mark.unit
class TestParseHfConfig:
    def test_exact_fingerprint_hit_returns_entry_name(self):
        table = _loader_configs_data(MATCH_JSON)
        first_name, first_params = next(iter(table.items()))
        hf_config = SimpleNamespace(**first_params)   # 逐键复刻表项作指纹
        model_name, quant_type = parse_hf_config(hf_config)
        assert model_name == first_name
        assert quant_type == "bf16"                   # 无 quantization_config

    def test_no_match_falls_back_to_model_type(self):
        hf_config = SimpleNamespace(model_type="my_unknown_model")
        model_name, _ = parse_hf_config(hf_config)
        assert model_name == "my_unknown_model"
```

3. **放置与运行**：在容器内 `cd components/omni-npu/tests`，先 `pytest unit/config/test_loader_match_hf.py -v` 单跑这个文件，再 `./run_tests.sh unit` 确认没有破坏存量用例，并查看 `--cov` 输出中 `config_loader/loader.py` 的覆盖率变化。
4. **核对**：如果第 1 个用例失败，最可能的原因是上游给 `match_hf_configs.json` 加了与首表项完全同指纹的新条目（多命中走 deepseek 特判或抛错）——这正是这个测试的价值：**锁定指纹表的唯一性契约**。

**预期结果**：新文件 4 个用例全绿，`./run_tests.sh unit` 总数 = 原有用例数 + 4。运行输出——待本地验证（编写环境无 torch_npu/vllm）。

**延伸**（对应实践任务的另一半）：如果你在 u2-l4 之后自己写过 patch，改靶子为该 patch 改写的纯函数，仿照 [components/omni-npu/tests/unit/vllm_patch/test_patch_dir_mapping.py:L7-L23](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/vllm_patch/test_patch_dir_mapping.py#L7-L23) 断言其输入输出；若 patch 目标涉及 vLLM 对象，则仿照 [components/omni-npu/tests/unit/connector/test_register.py:L28-L45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/connector/test_register.py#L28-L45) 的「路径常量 + patch + mock」三件套。

## 6. 本讲小结

- **组织规则**：测试树镜像源码树，命名 `test_<源文件名>.py`；`pytest.ini` 是权威配置（优先级高于 pyproject 的 `[tool.pytest.ini_options]`），marker 只被部分使用，**按目录筛选才是可靠口径**；文档（README/QUICKSTART/STRUCTURE）严重滞后于真实测试树，以目录树为准。
- **两道 conftest 防线**：顶层 conftest 猴子补丁过滤 `omni.*` entry points（隔离 omni-cache 等兄弟插件，是 u2-l1 发现机制的反向应用）并提供 `default_vllm_config` fixture；unit 子树 conftest 让 vLLM 自定义算子注册幂等化，防多模块重名注册。
- **分层依据**：unit 用 mock 验证逻辑契约（不需要 NPU 设备，但需要 torch_npu/vllm 软件包，最自然的运行处是推理容器）；integration 靠 `NPU_AVAILABLE` + `skipif_no_npu` 在无真机时自动跳过，多卡用例由 torchrun 单独拉起；`unit/layers/st/` 是「unit 目录里也要真机」的例外。
- **统一入口**：`run_tests.sh` 做参数分拣（`--` 透传、`--durations-out`）、依赖自装、git 快照与三分支拼装；生效的覆盖率配置是显式传入的 `tests/.coveragerc`（pytest.ini 里的 coverage 段是死配置）。
- **镜像内跑测**：多容器 CI 用 run_docker.sh 把 16 卡主机切成 4 个容器（各见 4 卡），ut_config.sh 集中维护容器↔卡映射与 pytest-split 分组参数，concurrent 脚本同步代码并行执行并回收日志/耗时/覆盖率三类工件。

## 7. 下一步学习建议

- **下一讲（u10-l3）**：二次开发的三类扩展点（新增 vLLM 运行时补丁、登记模型最佳实践配置、注册 KV connector）——本讲的「补一个单测」正是提交那些扩展时配套测试的模板。
- **推荐阅读源码**：`tests/unit/platform/test_platform.py`（看 NPUPlatform 的 check_and_update_config 契约如何被 mock 锁定，呼应 u2-l2）；`tests/unit/connector/test_llmdatadist_connector_v1.py`（KV connector 四协作类的单测组织，呼应 u4-l2）；`tests/ut_CI_check/ut_CI_parse_logs.py`（CI 如何把容器日志解析成结论）。
- **动手方向**：给 4.4 的多容器流程补一次真实演练——按 [components/omni-npu/tests/README.md:L134-L156](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/README.md#L134-L156) 在一台 16 卡机器上跑通 `run_docker.sh` + `concurrent_test_run_multi_docker.sh`，再用 `ut_CI_check_durations_balance.py` 检查四组负载是否均衡，体会「历史耗时驱动的分组」如何随测试集演化而失准、又如何用 `test_durations_merged.json` 回写修正。
