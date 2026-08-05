# 持续集成与 model_parity 门控

## 1. 本讲目标

本讲是「测试、调试与性能分析」单元的收尾讲。前置讲义 u7-l4 讲清了 **model_parity 是什么、为什么 SimX 必须与 RTL 保持 lockstep、以及「绝不放宽容差吸收差异」的纪律**；u13-l1 讲清了 **测试基础设施的组织方式**——把测试用例抽象成 N 维空间中的一个点，全部声明在 `ci/testcases/*.yaml` 目录里，由 `testcase.py + conftest.py + test_runner.py` 三件套驱动。

本讲要打开这三件套的「引擎盖」，让你读完后能够：

1. 说清 Vortex CI 的总架构：**测试是数据（YAML）＋ pytest 粘合层＋不变的执行器（blackbox.sh）**，以及为什么这套设计能彻底解决「驱动硬编码进每一行 bash」的旧病。
2. 读懂 `conftest.py` 如何把一条 YAML 记录机械地变成一个带标记的 pytest 测试项：动态注册 marker、按 ambient XLEN 过滤、`known_issue` 转 `xfail`、`sim_build` fixture 去重构建。
3. 读懂 `test_runner.py` 里 `_model_parity` 与 `_perf_gate` 两个断言函数的精确判定逻辑：退休指令必须**逐位相等**、周期必须在容差内、perf 基线如何被棘轮锁定。
4. 理解 `model_parity`/`perf_gate` 为何是「跨切面的专用 cell」，以及 GitHub Actions 如何按事件类型把测试扇出到矩阵单元。

---

## 2. 前置知识

阅读本讲前，请确保已理解以下概念（均来自前置讲义，本讲不再重复，只承接）：

- **SimX 与 RTL 的双引擎关系**：SimX 是 C++ 写的周期精确仿真器，RTL 是 Verilator 仿真的硬件实现。两者必须功能与时序一致（详见 u7-l4、u5-3）。
- **PERF 摘要行**：程序结束时，运行时会打印一行设备级摘要 `PERF: instrs=<N>, cycles=<N>, IPC=...`，其中 `instrs` 是各核退休指令之和、`cycles` 是各核周期的最大值（详见 u13-l3、u7-l4）。
- **驱动（driver）**：`simx`（便宜、C++ 模型）、`rtlsim`（贵、Verilator 仿真 RTL）、`xrtsim`/`opaesim`（FPGA 仿真）。详见 u1-l4、u3-l3。
- **测试是 N 维空间中的点**：一条测试由 `category/driver/xlen/config/shape/tier/needs/touches` 等维度确定，声明在 YAML 里（详见 u13-l1）。
- **CONFIGS 双侧生效纪律**：应用侧和驱动侧必须用相同的 `CONFIGS` 宏构建（详见 u1-l4、u13-l1）。

本讲用到但需要先点明的两个 pytest 概念：

| 术语 | 通俗解释 |
|------|----------|
| **marker（标记）** | 给测试贴的「标签」，如 `simx`、`rtlsim`、`smoke`、`model_parity`。用 `-m "simx and smoke"` 可按标签表达式筛选要跑的测试。 |
| **fixture（夹具）** | 测试运行前自动准备、运行后自动清理的「前置条件」，如「先把 sim 编译好」。 |
| **parametrize（参数化）** | 把同一个测试函数展开成多个测试项，每个用不同参数（这里是不同的测试用例）。 |
| **xfail（预期失败）** | 标记「这条测试我知道会失败」，失败不算红、意外通过会报 `XPASS`。 |

---

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 职责 | 本讲角色 |
|------|------|----------|
| `docs/designs/continuous_integration.md` | CI 总体设计文档（架构、动机、迁移记录） | 设计纲领，讲清「测试是数据」的来龙去脉 |
| `ci/testcase.py` | `Spec` 模型 + YAML 加载器 + 规划 CLI（`lint`/`matrix`/`select`/`drivers`） | 把 YAML 翻译成具体测试用例对象；本讲的「数据层」 |
| `ci/conftest.py` | pytest 钩子与 fixture | 把 `Spec` 变成 pytest 测试项；本讲的「粘合层」 |
| `ci/test_runner.py` | 唯一的测试函数 `test_case` + `_model_parity`/`_perf_gate` 断言 | 真正跑测试、做断言；本讲的「执行层」 |
| `ci/perf_baseline.py` | perf 基线的读写 | perf_gate 的黄金数据管理 |
| `ci/testcases/model_parity.yaml` | 通用流水线 parity 用例 | model_parity 的具体样例 |
| `.github/workflows/ci.yml` | GitHub Actions 工作流 | 矩阵扇出与门控 |
| `AGENTS.md` | 开发纪律（§4 测试规则） | parity/baseline 的不可逾越红线 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① CI 的目录驱动模型（设计纲领）；② `conftest.py` 的 pytest 粘合机制；③ `test_runner.py` 的 parity/perf_gate 断言逻辑；④ check 用例的门控纪律与 GitHub 扇出。

### 4.1 CI 目录驱动模型：测试是数据，不是代码

#### 4.1.1 概念说明

Vortex CI 经历过一次大重构。旧引擎 `ci/regression.sh.in` 是约 1400 行命令式 bash，把驱动名（`simx`/`rtlsim`/...）**硬编码进每一行**——全文件有 401 处驱动写死的调用。这带来一堆病：想只跑 simx 必须改 401 行；测试覆盖度无法被查询；构建与运行纠缠在一起（相邻两行 `CONFIGS` 不同就会重复 elaborate 仿真器）。

重构的核心思想只有一句：**把测试从「代码」变成「数据」**。把测试用例写成结构化的 YAML 记录，让「筛选」「定时」「报告」都变成对数据的查询，而不是改 bash。`blackbox.sh`（实际执行器，详见 u1-l4）保持不变——重构只动外层编排，不动执行原语。

#### 4.1.2 核心流程

整个引擎只有三样东西是 Vortex 自己写的，其余全部交给 pytest 这个工业标准：

```
ci/testcases/*.yaml        marker/-m, --changed     ┌──────────────┐
(数据: 测试用例)  ───────────────────────────────▶  │   pytest     │
                   testcase.py + conftest.py + test_runner.py (运行器)│
                                                      └──────┬───────┘
                                        fixture: 每个构建键构建一次 │ 多个用例复用
                                                     ┌────▼────────┐  运行
                                                     │  执行器     │  每用例
                                                     │ blackbox.sh │  (不变)
                                                     └────┬────────┘
                                          --junitxml ┌────▼────────┐
                                                     │  报告器     │ → GitHub 测试报告
                                                     └─────────────┘
```

设计文档把一条测试用例建模为「N 维空间中的一个点」，这些维度今天被压扁在一行 bash 里，但显式保留后任何一个都能变成**筛选条件**或**矩阵维度**：

```
category   amo, cache, tensor, graphics, …
driver     simx | rtlsim | xrtsim | opaesim          (成本轴)
xlen       32 | 64                                    (构建树轴)
config     CONFIGS="-DVX_CFG_…"                        (重建轴)
shape      cores/warps/threads/l2/l3, args
tier       smoke | full | nightly                     (何时跑轴)
needs      (无) | mpi | sst | gem5                    (环境轴)
touches    本用例触及的源码路径                        (选择轴)
```

#### 4.1.3 源码精读

设计文档开篇就点明了三段式分工与「测试是数据」的定位：

> [docs/designs/continuous_integration.md:1-11](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/continuous_integration.md#L1-L11) — 说明 Vortex 测试是「声明式数据，由 pytest 运行」，`blackbox.sh` 保持不变的执行器，`regression.sh` 退化为本地入口 + 四个特殊 host 流程的后端。

§2 给出了 N 维空间模型，是理解后续一切筛选逻辑的根基：

> [docs/designs/continuous_integration.md:40-54](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/continuous_integration.md#L40-L54) — 列出 8 个轴，并说明它们今天被压在一行 bash 里、显式化后任一可作筛选器或矩阵维度。

一个具体的 YAML 类别文件长这样（字段与 `blackbox.sh` 的 flag 一一对应，是忠实转写而非重新解读）：

> [ci/testcases/model_parity.yaml:36-40](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/model_parity.yaml#L36-L40) — `vecadd` 用例：`check: model_parity` 标明它是跨驱动校验用例，`via: blackbox` 指定执行方式，`app: vecadd` + `args: -n16384` 是程序与参数。

注意 YAML 头部的纪律说明非常关键，它讲清了「工作负载要够大」这条容易被忽视的要求：

> [ci/testcases/model_parity.yaml:8-15](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/model_parity.yaml#L8-L15) — 说明负载要 ≥~300k 周期让稳态占主导（太小的 kernel 全是启动/派发偏斜，会让 gap 比例变噪声）；并强调「绝不放宽容差来掩盖真实回归」。

#### 4.1.4 代码实践

1. **实践目标**：建立「测试是数据」的直觉，会用 marker 表达式筛选测试。
2. **操作步骤**：
   - 在已 `configure` 过的 `build/` 目录里，列出所有类别文件：`ls ci/testcases/*.yaml`。
   - 用 pytest 的「只收集不运行」做干跑，看一个 marker 会选中哪些用例：
     ```bash
     cd build32   # 或你的构建目录
     VX_XLEN=32 python3 -m pytest ../ci --collect-only -q -m "model_parity"
     ```
3. **需要观察的现象**：`--collect-only` 不实际跑测试，只打印被选中的测试项 ID 列表（形如 `model_parity:vecadd:rtlsim`）。
4. **预期结果**：能看到通用流水线的 parity 用例（vecadd、sgemm、softmax、stencil3d、raycast 等）被列出；每个 `check:` 用例只生成一个测试项（不像普通用例按 driver 展开）。
5. 若本地未完成工具链安装，`--collect-only` 仍可工作（它不编译、不运行），是安全的浏览方式；实际跑 rtlsim 才需要完整工具链——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么旧 bash 引擎「驱动硬编码进每一行」会让「只跑 simx」变得困难？
**答案**：因为每一行调用都把 `--driver=<d>` 写死了，没有单一的可筛选缝隙。要把 simx 单独抽出来，必须改 401 行。而把测试变成数据后，`--drivers=simx` 退化成一条 marker 查询 `-m "simx"`，一行搞定。

**练习 2**：YAML 里 `configs` 与 `configs+` 有什么区别？
**答案**：`configs` **覆盖**类别默认值；`configs+` 在默认值基础上**追加**。这由 `testcase.py` 的 `_merge_configs` 实现（[ci/testcase.py:167-173](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L167-L173)）。

---

### 4.2 pytest 粘合层：conftest.py 如何把数据变成测试项

#### 4.2.1 概念说明

`conftest.py` 是 pytest 约定的「钩子与 fixture」寄存处——pytest 启动时会自动发现并执行它，无需任何配置文件。Vortex 没有写 `pyproject.toml`/`pytest.ini`，全部靠 `conftest.py` 动态注册 marker 和参数化测试。这个文件要回答的核心问题是：**如何让一份 YAML 数据，无需任何手写映射，就能变成一组带正确标签的 pytest 测试项？**

它用了四个 pytest 钩子协同完成这件事，下面逐一拆解。

#### 4.2.2 核心流程

```
pytest 启动
  │
  ├─ pytest_configure(config)
  │     遍历所有用例的 markers() → 动态注册成 pytest marker
  │     （新增类别/driver 无需改本文件；配合 --strict-markers 可抓 -m 拼写错误）
  │
  ├─ pytest_generate_tests(metafunc)
  │     for 每个用例:
  │       若 applies_to_xlen(ambient_xlen) 为假 → 跳过（ambient XLEN 过滤）
  │       收集 markers；若 known_issue 非空 → 追加 xfail(strict=False)
  │       用 pytest.param(case, marks=..., id=case.id) 参数化
  │
  ├─ sim_build fixture（每个用例运行前）
  │     if via==blackbox → 返回 None（blackbox 自己构建）
  │     else 按 (driver, configs) 构建键去重：构建过就跳过，否则 make -C sim/<d>
  │
  └─ test_runner.test_case(case, sim_build, request)   ← 真正的测试（见 4.3）
```

关键设计点：**marker 是从数据派生的**。新增一个类别或驱动，只要在 YAML 里写出来，`conftest.py` 自动注册对应 marker，无需改任何 Python 代码。

#### 4.2.3 源码精读

**钩子①：动态注册 marker**。`pytest_configure` 遍历所有用例、收集它们的 marker 集合、逐个注册：

> [ci/conftest.py:42-47](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/conftest.py#L42-L47) — 从数据派生 marker 并注册，配合 `--strict-markers` 把拼错的 `-m` 表达式变成硬错误而非「静默选中空集」。

> [ci/testcase.py:118-126](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L118-L126) — `Spec.markers()` 生成每个值一个 marker：类别、tier、driver、`check`（即 `model_parity`/`perf_gate`），外加 `needs_<n>`。这就是 marker 的唯一来源。

**钩子②：参数化 + ambient XLEN 过滤 + known_issue→xfail**：

> [ci/conftest.py:50-67](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/conftest.py#L50-L67) — 核心：只收集适用当前 XLEN 的用例（`applies_to_xlen`）；带 `known_issue` 的用例追加 `xfail(strict=False)`——它仍会构建并运行（保留日志、意外通过显示为 XPASS），但失败不会让 CI 变红。

`ambient_xlen` 决定「跑在哪棵构建树里」：

> [ci/conftest.py:22-24](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/conftest.py#L22-L24) — 从环境变量 `VX_XLEN`（默认 32）读当前构建树的位宽，这正是 `regression.sh` 传入的 `VX_XLEN=$XLEN`（见 [ci/regression.sh.in:355-359](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/regression.sh.in#L355-L359)）。`xlen` 是**外层维度**——在收集期过滤，而不是展开（build32/build64 是两棵独立的树）。

**fixture：sim_build 去重构建**。这是旧引擎 P4 病（构建与运行纠缠）的修复点：

> [ci/conftest.py:74-102](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/conftest.py#L74-L102) — `sim_build` 按 `(driver, configs)` 构建键去重，同一个键只构建一次（build-once-run-many）；但 `via: blackbox` 用例直接返回 `None`——因为 blackbox 自己在运行时用完整 flag 集（configs + shape 派生的 `-DVX_CFG_NUM_*`）构建，fixture 预构建反而会编出一个 shape 被剥离的配置。

值得注意的还有「先 clean 再构建」的纪律：

> [ci/conftest.py:92-96](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/conftest.py#L92-L96) — 新 `CONFIGS` 必须先 `make clean`，不能复用上一个配置的 `obj_dir`（残留的 Verilator 状态会产生虚假 lint 错误）。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到一条 YAML 记录如何展开成带 marker 的测试项。
2. **操作步骤**：
   ```bash
   cd build32
   # 看一条 known_issue 用例（如 sgemv）是否被标了 xfail
   VX_XLEN=32 python3 -m pytest ../ci --collect-only -q -m "model_parity" -v
   ```
3. **需要观察的现象**：输出里每个测试项后会有 marker 标注；带 `known_issue` 的用例（如 `sgemv`、`sgemm-mc`）会显示 `xfail`。
4. **预期结果**：`sgemv` 这条会标注 `xfail(reason='known issue: cycle-parity gap ~14%...')`，说明它的失败被预期、不会让 CI 变红；而 `vecadd`、`stencil3d`、`raycast` 不带 xfail，必须真过。
5. 实际运行行为**待本地验证**（需工具链）；`--collect-only` 部分可在仅有 PyYAML 的环境验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `known_issue` 用例用 `xfail(strict=False)` 而不是直接 `skip`？
**答案**：`skip` 完全不运行，覆盖会「静默缺失」。`xfail` 仍会构建并运行：失败被预期（不红），但日志保留；一旦底层支持落地、用例意外通过，会以 `XPASS` 显式浮现，提醒维护者移除 `known_issue`。这呼应文档「aspirational coverage is tracked, not silently absent」。

**练习 2**：为什么 blackbox 用例要让 fixture 返回 `None`、由 blackbox 自己构建？
**答案**：blackbox 运行时用的是 configs **加上** shape 派生的 `-DVX_CFG_NUM_*` 完整 flag 集；fixture 只用 configs 预构建会编出一个「shape 被剥离」的配置（如 `NUM_TEX_CORES` 配不上核数），反而触发虚假的 Verilator 宽度 lint，而且 blackbox 照样会重建。让运行自己拥有构建更干净（详见 [ci/conftest.py:79-90](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/conftest.py#L79-L90)）。

---

### 4.3 test_runner.py：model_parity 与 perf_gate 的断言逻辑

#### 4.3.1 概念说明

`test_runner.py` 是整个引擎里**唯一**的测试函数 `test_case`。它做的事很简单：根据用例的 `check` 字段分流——普通用例跑一遍断言退出码为 0；`model_parity` 用例跑两条腿（simx + rtlsim）并对比；`perf_gate` 用例跑一遍 rtlsim 与黄金基线对比。

这个模块的精髓在于两个断言函数把 u7-l4 讲的「parity 判定标准」落成了**精确的代码**：退休指令逐位相等、周期在容差内。读懂这两段代码，就彻底理解了 model_parity 的判定机制。

#### 4.3.2 核心流程

**model_parity 的判定流程**（一条用例内部跑两条腿）：

```
_model_parity(case, xlen):
  (simx_instrs, simx_cycles) = _run_one(case, xlen, "simx")   # 腿1: C++ 模型
  (rtl_instrs,  rtl_cycles ) = _run_one(case, xlen, "rtlsim") # 腿2: RTL 仿真
  gap = |rtl_cycles - simx_cycles| / rtl_cycles               # 以 RTL 为基准的相对差
  打印 PARITY: 行（两侧 instrs/cycles + gap + tolerance）
  assert simx_instrs == rtl_instrs        # 断言①: 功能逐位一致
  assert gap <= case.tolerance            # 断言②: 周期在容差内（默认 5%）
```

其中每条腿 `_run_one` 跑 blackbox、捕获输出、用正则抠出**最后一行** `PERF:` 摘要：

```
_PERF_RE = re.compile(r"^PERF: instrs=(\d+), cycles=(\d+), IPC=", re.M)
```

**perf_gate 的判定流程**（与黄金基线对比，注意是**双向**门控）：

```
_perf_gate(case, xlen, update):
  (instrs, cycles) = _run_one(case, xlen, "rtlsim")
  if update: record(...) 并返回            # 记录模式（人手工跑，CI 永不开）
  base = 加载该类别基线; 取本用例 + 本 xlen 的参考值
  assert config_hash 一致                  # 防陈旧：运行配置变了要重生成
  assert instrs == ref.instrs              # 防工作负载变了
  ratio = cycles / ref.cycles
  assert ratio <= 1 + 2%                   # 上界: 回归（硬失败）
  assert ratio >= 1 - 2%                   # 下界: 改进超棘轮（也要失败，要求锁定收益）
```

#### 4.3.3 源码精读

**正则与单腿运行**：

> [ci/test_runner.py:29-44](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py#L29-L44) — `_PERF_RE` 只匹配设备级摘要行（带 `coreN:` 前缀的逐核行不匹配）；`_run_one` 用 `case.run_command(xlen, driver=driver)` 跑指定驱动、断言退出码为 0、抠出**最后一个** PERF 摘要的 `instrs/cycles`。`driver=` 参数是关键——它覆盖用例自身的驱动，让一条用例能跑两条腿。

**model_parity 双腿对比**——本讲最核心的代码：

> [ci/test_runner.py:47-60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py#L47-L60) — 先跑 simx 再跑 rtlsim，`gap` 以 **rtlsim 周期为基准**计算相对差（`abs(rtl-simx)/rtl`），打印 `PARITY:` 行（绿灯也留趋势痕迹），然后两条断言：instrs 必须完全相等（不等即功能发散），gap 不得超过容差。

容差的默认值与可覆盖性定义在数据层：

> [ci/testcase.py:42-43](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L42-L43) — `DEFAULT_PARITY_TOLERANCE = 0.05`（5%），可在 YAML 用 `tolerance:` 字段或 `defaults.tolerance:` 覆盖。

> [ci/testcase.py:85-88](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L85-L88) — 两条重要约束：① `model_parity`/`perf_gate` 用例被强制钉死在 `xlen=[32]`（SimX 时序模型与 perf 基线都只针对 RV32 rtlsim 验证，RV64 不门控）；② 容差从用例/默认/5% 三级取值。

**关键机制：check 用例不做驱动展开**。普通用例若写 `drivers: [simx, rtlsim]` 会展开成两个测试项；但 `check:` 用例永远是**一个**测试项（钉在 rtlsim），由 runner 自己在内部跑 simx 这条腿：

> [ci/testcase.py:196-204](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L196-L204) — `load_category` 里：带 `check` 的条目强制 `drivers=["rtlsim"]`（因为 rtlsim 才 elaborate RTL，构建/矩阵落位正确），runner 用 `driver=` override 自己驱动 simx 当第二条腿。

`run_command` 的 driver override 正是支撑这一点的接口：

> [ci/testcase.py:136-149](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L136-L149) — `run_command(xlen, driver=None)`：`driver` 覆盖用例自身驱动，parity runner 借此让同一条用例跑两种驱动。

**分发入口**：

> [ci/test_runner.py:94-104](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py#L94-L104) — `test_case` 按 `case.check` 分流：`model_parity` 走双腿、`perf_gate` 走基线、其余走单次运行断退出码。

**perf_gate 双向门控与防陈旧**：

> [ci/test_runner.py:63-91](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py#L63-L91) — perf_gate 的四道断言：① `config_hash` 必须匹配（运行配置变了→报「重生成」而非对比陈旧数字）；② `instrs` 必须匹配（工作负载变了→重生成）；③ ratio ≤ 1+容差（回归硬失败）；④ ratio ≥ 1−容差（改进超棘轮也要失败，逼你更新基线锁定收益）。容差见 [ci/perf_baseline.py:19-23](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/perf_baseline.py#L19-L23)（±2%）。

#### 4.3.4 代码实践

1. **实践目标**：把 `_model_parity` 的判定逻辑和一条真实 YAML 用例对应起来，能逐字段说清「它怎么断言退休指令一致、周期在容差内」。
2. **操作步骤**：
   - 打开 [ci/testcases/model_parity.yaml:36-40](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/model_parity.yaml#L36-L40)（`vecadd` 用例）。
   - 对照 [ci/test_runner.py:47-60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py#L47-L60) 的 `_model_parity`，手工推演：用例 app=`vecadd`、args=`-n16384`、未设 `tolerance`（故取默认 5%）。
   - 推演它会被展开成**一个**测试项 `model_parity:vecadd:rtlsim`（因 `check:` 不做驱动展开，见 [ci/testcase.py:196-204](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L196-L204)），运行时内部跑 simx 与 rtlsim 两条腿。
3. **需要观察的现象**：如果本地能跑（需完整工具链），命令
   ```bash
   cd build32
   VX_XLEN=32 python3 -m pytest ../ci -m "model_parity and vecadd" -v -s
   ```
   会打印一行 `PARITY: model_parity:vecadd:rtlsim: instrs simx=<A> rtlsim=<A>, cycles simx=<C1> rtlsim=<C2>, gap=<G%> (tolerance 5%)`。
4. **预期结果**：两侧 `instrs` 数字相同（功能一致），gap ≤ 5%（vecadd 在当前 YAML 里没有 `known_issue`，必须真过）。
5. 完整 rtlsim 运行**待本地验证**（依赖工具链与较长仿真时间）；源码推演部分不需要运行即可完成。

#### 4.3.5 小练习与答案

**练习 1**：`gap` 为什么用 `abs(rtl_cycles - simx_cycles) / rtl_cycles`，分母是 rtlsim 而不是平均值？
**答案**：以 RTL 为基准衡量 SimX 模型的偏差，语义是「SimX 与真实 RTL 差了百分之几」。这是一个明确的方向约定，让容差比较的口径稳定。

**练习 2**：`_run_one` 为什么取 `perf[-1]`（最后一个 PERF 摘要）而不是 `perf[0]`？
**答案**：程序里可能有多个 PERF 行（如逐核行带 `coreN:` 前缀已被正则排除，但仍可能多次 dump），设备级最终摘要是**最后一行**，它才是整个运行的累计汇总（`instrs` 求和、`cycles` 取最大）。

**练习 3**：perf_gate 的下界断言（`ratio >= 1 - 2%`）为什么会「改进也失败」？
**答案**：这是**棘轮（ratchet）**机制——一次超出 2% 的改进必须更新基线把收益锁死，否则后来一次悄悄回归到旧数字仍会被当成「在容差内」而漏网。它逼你把 perf 改进显式化、可评审（见 4.4.3 的基线更新纪律）。

---

### 4.4 门控纪律与 GitHub 矩阵扇出

#### 4.4.1 概念说明

前三个模块讲的是「引擎怎么跑一条用例」。本模块讲两个宏观问题：① `model_parity`/`perf_gate` 在整个测试矩阵里如何**不重复运行**（跨切面的专用 cell）；② 这些测试在 GitHub Actions 上如何按事件类型**扇出**到不同的 runner。

这两个问题共同决定了「parity 检查什么时候跑、在哪跑、跑几遍」。

#### 4.4.2 核心流程

**专用 cell 原则**：每个 `check: model_parity` 用例都自带 `model_parity` marker。GitHub 矩阵里有一个 `model_parity` cell，它跑 `-m "model_parity and rtlsim"`，于是**横扫整个目录里所有 parity 用例**（通用流水线 + 各扩展自己的 `model_parity-*` 用例），形成「一个集中的 simx↔RTL 门」。为避免同一条 parity 用例在它所属类别的 cell 里又跑一遍，所有非 check 类别的 cell 都在 marker 表达式里加上 `and not model_parity and not perf_gate` 把它们排除。

**GitHub 扇出**（按事件类型的驱动/tier 策略）：

```
事件类型        驱动              tier          含义
push           simx              smoke         便宜高信号，先挡明显错误
pull_request   simx, rtlsim      smoke, full   PR 门加上 RTL 校验
schedule       全部               全部           nightly/weekly 跑全套
```

关键洞察：**push 只跑 simx**（把约 168 次昂贵的 rtlsim 推迟到 PR 门与 nightly）。但如果改的是 RTL 相关路径，即便在 push 上也会被强制升级加入 rtlsim（因为 simx 是独立 C++ 模型，无法检验 RTL 改动）。

#### 4.4.3 源码精读

**专用 cell 与排除逻辑**在 ci.yml 的 tests job 里：

> [.github/workflows/ci.yml:205-215](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/.github/workflows/ci.yml#L205-L215) — 每个 cell 跑 `pytest ci -m "<category> and <driver>"`；`model_parity`/`perf_gate` 类别正常，其余类别一律追加 `and not model_parity and not perf_gate`，确保分散在各类别文件里的 check 用例只在专用 check cell 里运行一次。

设计文档对「专用 cell」的解释：

> [docs/designs/continuous_integration.md:197-204](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/continuous_integration.md#L197-L204) — `model_parity` 类别是一个专用 cell：因为它所有用例都带 `model_parity` marker，该 cell 跑 `-m "model_parity and rtlsim"` 横扫全目录 parity 用例，跨 `{xlen 32}`，在 `full` tier（rtlsim 重→PR+nightly）。所以 parity 用例绝不重复跑。

**驱动/tier 按事件策略**：

> [.github/workflows/ci.yml:76-82](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/.github/workflows/ci.yml#L76-L82) — plan job 按事件名设定 `DRIVERS`/`TIER`：schedule 全跑、PR 跑 simx+rtlsim 的 smoke+full、push 只跑 simx 的 smoke。

**RTL 改动强制升级驱动**（path→driver 升级）：

> [.github/workflows/ci.yml:100-109](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/.github/workflows/ci.yml#L100-L109) — 调 `ci/testcase.py drivers --changed-from=<base>` 算出 diff 触发了哪些驱动；不确定的 diff（新分支/force-push）升级为全覆盖。

升级规则本身定义在数据层：

> [ci/testcase.py:249-262](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L249-L262) — `_DRIVER_PATHS` 把路径前缀映射到强制驱动：`hw/rtl/`→rtlsim、`hw/rtl/afu/`→额外 xrt+opae、`VX_config.toml`/`VX_types.toml`→rtlsim（配置输入会重新生成 RTL 参数）。核心理由：RTL 被所有 sim 后端共享，simx 是独立 C++ 模型无法检验它，所以纯 simx 覆盖等于没测。

**两条不可逾越的纪律**（来自 AGENTS.md）：

> [AGENTS.md:89](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L89) — SimX 是 RTL 的时序模型，任何挪动 RTL 周期的改动必须连同 SimX 时序模型更新一起提交；`model_parity` 门强制这一点；**绝不放宽容差来吸收差异，而要去建模该行为**。

> [AGENTS.md:88](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L88) — perf 基线 `ci/perf/baselines/*.json` 是黄金数据，**永不手改**、永不靠改数字「修」红 perf 门；只能由人手工跑 `--update-baselines` 重生成并评审；CI 永远不能传这个 flag。

一条 `known_issue` parity 用例的真实样例（说明纪律：用 known_issue 而非放宽 tolerance 记录已知的、在调查中的 gap）：

> [ci/testcases/model_parity.yaml:54-64](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/model_parity.yaml#L54-L64) — `sgemv` 用例：执行通过、退休指令精确一致、只有周期发散约 14%（RTL 更慢）。原因是 SimX 没建模解耦的 LSU pending pool，欠计了该 kernel 受限的流式 load 延迟。**用 `known_issue` 标注**而非放宽容差，pending SimX LSU 时序对齐。

#### 4.4.4 代码实践

1. **实践目标**：理解 RTL 改动如何强制升级驱动覆盖，并能预判一次 diff 会触发哪些 cell。
2. **操作步骤**：
   - 阅读 [ci/testcase.py:249-262](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L249-L262) 的 `_DRIVER_PATHS` 表。
   - 假设你改了一个文件 `hw/rtl/core/VX_alu_unit.sv`，用规划 CLI 预测会强制哪些驱动：
     ```bash
     # 在仓库根目录，模拟一个 base（仅用于演示 drivers 子命令）
     python3 ci/testcase.py drivers --changed-from=HEAD~1
     ```
3. **需要观察的现象**：因为改动落在 `hw/rtl/` 前缀下，输出应包含 `rtlsim`（若 HEAD~1..HEAD 实际未触及该文件，则可能输出 ALL 或空——以真实 diff 为准）。
4. **预期结果**：`hw/rtl/core/...` 命中 `("hw/rtl/", ("rtlsim",))` 规则，即便在只跑 simx 的 push 上也会被升级加入 rtlsim。若改动在 `hw/rtl/afu/` 还会额外加 `xrt`、`opae`。
5. 子命令 `drivers` 仅需 PyYAML + git，**可在源码树直接运行**；但它读真实 git diff，故具体输出依赖你本地的改动，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 parity 用例要做成「专用 cell」而不是让每个类别的 cell 自己跑本类的 parity 用例？
**答案**：避免重复运行。若不排除，一条分散在 `tensor.yaml` 里的 `model_parity-fp16` 用例会在 `tensor` cell 跑一次、又在 `model_parity` cell 跑一次——rtlsim 极慢，重复跑纯属浪费。专用 cell 用 `-m "model_parity and rtlsim"` 一次性横扫全目录所有 parity 用例，其余 cell 用 `and not model_parity` 排除。

**练习 2**：为什么改 `VX_config.toml` 会强制加入 rtlsim？
**答案**：toml 是硬件配置的单一真相来源（详见 u2-1），改它会重新生成 RTL 参数（重新 elaborate 仿真器），属于 RTL 范畴的改动。simx 是独立 C++ 模型无法检验 RTL elaborate 后的行为，必须靠 rtlsim（见 [ci/testcase.py:258-260](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L258-L260)）。

**练习 3**：发现一条 parity 用例周期差了 8%，最快的「修绿」是改大它的 `tolerance`，这样做对吗？
**答案**：不对。AGENTS.md §4 明令「绝不放宽容差来吸收差异」。8% 的 gap 说明 SimX 时序模型与 RTL 失配（或有未建模的效应），正确做法是根因分析、在 SimX 侧补齐时序建模，或用 `known_issue` 记录一个在调查中的 tracked gap（如 `sgemv` 那样），等模型对齐后再移除。

---

## 5. 综合实践

设计一个贯穿本讲的练习：**为一次真实的 RTL 改动，端到端走一遍 CI 门控会怎么判定。**

**背景**：假设你修改了 `hw/rtl/core/VX_alu_unit.sv`，让某条 ALU 指令的时序从 1 拍变成 2 拍（合理的微架构调整），并且按 AGENTS.md 纪律**同步更新了 SimX 对应单元的时序模型**。

请按顺序完成：

1. **预测驱动升级**：用 `python3 ci/testcase.py drivers --changed-from=<base>` 确认这次改动会强制加入 `rtlsim`（因落在 `hw/rtl/` 下）。解释为什么 push 默认只跑 simx 在这里不安全。

2. **定位会受影响的 parity 用例**：阅读 [ci/testcases/model_parity.yaml:31-33](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/model_parity.yaml#L31-L33) 的 `touches` 字段——它声明 `sim/simx/` 与 `hw/rtl/`，说明任何 core-model 或 RTL 改动都应触发这套 parity。在本地（若工具链就绪）跑：
   ```bash
   cd build32
   VX_XLEN=32 python3 -m pytest ../ci -m "model_parity" -v -s
   ```
   观察 `PARITY:` 行里 `vecadd` 这类纯 ALU/LSU streaming 用例的 gap。

3. **解读判定**：对照 [ci/test_runner.py:47-60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py#L47-L60) 回答：
   - 如果你的 SimX 时序模型**也**同步改成了 2 拍，预期 `instrs` 相等、gap 仍在 5% 内 → 绿灯。
   - 如果你**忘了**同步 SimX，instrs 仍相等（功能没变），但 cycles 会偏离 → 要么 gap 超 5% 变红（说明差异太大），要么靠 `known_issue` 暂记。两条路都不应该靠「改大 tolerance」来走通。

4. **（可选）perf_gate 联动**：这条 ALU 改动也会移动 rtlsim 周期数，从而触发 perf_gate。阅读 [ci/test_runner.py:63-91](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py#L63-L91)，说明为何此时需要人手工跑 `pytest ci -m perf_gate --update-baselines` 并评审 `ci/perf/baselines/*.json` 的 diff（如 `cycles: 292046 → 310xxx`），而 CI 自己永远不会自动更新基线。

**产出**：一张表，列出「改动文件 → 强制驱动 → 触发的 parity 用例 → 预期判定（绿/红/known_issue）→ 是否需要更新 perf 基线」。

> 注：步骤 2、4 的实际运行依赖完整工具链与较长 rtlsim 仿真时间，**待本地验证**；步骤 1、3 的源码推演不需要运行即可完成。

---

## 6. 本讲小结

- Vortex CI 的核心重构思想是**「测试是数据，不是代码」**：测试用例声明在 `ci/testcases/*.yaml`，`blackbox.sh` 保持不变的执行器，筛选/定时/报告全部变成对数据的查询，一举消除「驱动硬编码进 401 行 bash」的旧病。
- 引擎只需三个自写文件：`testcase.py`（数据模型 + 规划 CLI）、`conftest.py`（pytest 钩子/fixture）、`test_runner.py`（唯一测试函数），其余全部复用 pytest 工业标准，无需任何配置文件。
- `conftest.py` 把一条 YAML 机械地变成带标记的测试项：**从数据派生 marker**、按 ambient XLEN 过滤、`known_issue` 转 `xfail(strict=False)`、`sim_build` 按 `(driver,configs)` 去重构建（blackbox 用例除外）。
- `model_parity` 的判定是两条断言：**退休指令必须逐位相等**（不等即功能发散）、**周期 gap 不得超过容差**（默认 5%，`gap=|rtl−simx|/rtl`）。check 用例不做驱动展开，由 runner 内部跑 simx + rtlsim 两条腿。
- `perf_gate` 是**双向**门控：回归（cycles 升超 2%）硬失败，改进（降超 2%）也失败以棘轮锁定收益；基线是黄金数据，只能人手工 `--update-baselines` 重生成并评审，CI 永不自动更新。
- `model_parity`/`perf_gate` 是**跨切面的专用 cell**：一个 `-m "model_parity and rtlsim"` cell 横扫全目录所有 parity 用例，其余类别 cell 用 `and not model_parity and not perf_gate` 排除以避免重复。GitHub 按事件类型扇出（push 只跑 simx，PR 加 rtlsim，nightly 全跑），RTL 相关改动强制升级驱动。

---

## 7. 下一步学习建议

- **回到 parity 的调试侧**：若 parity 门红了，下一步是用 u13-l2 的 **SimX-as-oracle** 方法论——让 SimX 镜像 RTL 结构、两侧加匹配的 CSV trace dump、diff trace 定位首处分歧。本讲的 model_parity 门是「平时保持一致」的纪律，u13-l2 是「一旦不一致」的排障流程，两者一体两面。
- **性能深挖**：结合 u13-l3 的性能计数器与 roofline 分析，理解 perf_gate 基线背后的微架构指标（调度器利用率、流水线停顿、内存延迟），判断一次 perf 变动是算力受限还是带宽受限。
- **扩展 parity 覆盖**：若要为新增硬件功能加 parity 用例，阅读 `ci/testcases/tensor.yaml` 里的 `model_parity-fp16` 等扩展用例（[ci/testcases/tensor.yaml:162-171](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/tensor.yaml#L162-L171)）作为模板，在自己的类别文件里加一条 `check: model_parity`、只开该扩展，使回归可归因。AGENTS.md §4 要求「添加硬件功能时，必须同时添加或扩展一条 parity 用例」。
- **二次开发的 CI 视角**：u14-l3（扩展 Vortex）会讲到新增自定义加速器需在 SimX/RTL/kernel/config 四层协同改动；本讲告诉你这类改动会被 `_DRIVER_PATHS` 强制升级到 rtlsim 并被 parity 门检验，是扩展工作流的质量兜底。
