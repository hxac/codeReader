# cocotb 测试工具链（pytest + cocotb + GHDL + ruckus）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 SURF 回归测试栈的四块拼图（ruckus、GHDL、cocotb、pytest）各自负责什么、彼此如何衔接。
- 理解 `make MODULES=$PWD import` 与 `build/SRC_VHDL` 源缓存的关系，知道为什么每次跑测试前可能需要先 import。
- 看懂 `tests/common/regression_utils.py` 里的 `run_surf_vhdl_test` 如何把一条 pytest 用例翻译成一次 GHDL 仿真，并理解它对 `cocotb_test.simulator.run` 做了哪些封装。
- 会用 `pytest -n auto --dist=worksteal` 跑并行回归，理解参数扫描如何变成独立的仿真构建目录。

本讲是单元九「验证方法论与软件集成」的第一讲，承接 u1-l2「构建与仿真工具链」。u1-l2 讲的是 lint/语法分析那条线（`make ... analysis`），本讲讲的是回归测试这条线（`make ... import` + pytest），两条线共用同一套 ruckus + Makefile + GHDL 基础设施。

## 2. 前置知识

在进入源码前，先用一句话建立四块拼图的直觉：

| 拼图 | 角色 | 一句话职责 |
|------|------|-----------|
| **ruckus** | 源码收集 | 读遍所有 `ruckus.tcl`，把全仓库要进构建的 `.vhd` 文件收集、展平成一份源清单。 |
| **GHDL** | 仿真器 | 开源的 VHDL 仿真器，把源清单编译、elaborate 成可仿真电路。 |
| **cocotb** | 激励与断言 | 用 Python 协程驱动仿真器的 DUT 端口、在拍级别注入激励、做断言。 |
| **pytest** | 发现与调度 | 发现测试文件、对参数扫描展开成一个个用例、并行调度多个仿真进程。 |

还有一个关键的「胶水」库 **cocotb-test**：cocotb 本身假设由仿真器（makefile）来启动，而 `cocotb-test` 提供了 `cocotb_test.simulator.run(...)`，让 Python（也就是 pytest）能**反过来**启动仿真器。这条「pytest → cocotb-test → GHDL」的调用方向，是理解整条回归链的钥匙。

读者还应回顾三个 u1-l2 的结论：根 `Makefile` 极短，只设参数后 `include` 外部 `ruckus/system_ghdl.mk`；`GHDL_BASE_FLAGS` 固定为 `--std=08 --ieee=synopsys -frelaxed-rules -fexplicit`；CI 把 lint/test/docs 拆成三条并行 job。本讲深入的就是其中的 **test** job。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `Makefile` | 极短的根构建文件，设好 `MODULES`、`GHDL_BASE_FLAGS` 等，再 `include` ruckus 的 `system_ghdl.mk`，由此获得 `analysis` 与 `import` 两个目标。 |
| `ruckus.tcl` | 顶层构建总清单，用 `loadRuckusTcl` 加载七大 HDL 子树；`import` 时它决定了哪些源进缓存。 |
| `tests/common/regression_utils.py` | 回归测试的「公共底盘」：源缓存读取、环境变量解析、参数扫描工具、以及核心封装 `run_surf_vhdl_test`。 |
| `tests/README.md` | 回归测试风格指南，规定文件布局、`Test methodology` 头、参数扫描、断言与定时的写法。 |
| `pytest.ini` | 极简 pytest 配置，目前只声明 `norecursedirs = tests/legacy`。 |
| `pip_requirements.txt` | 锁定回归栈依赖（cocotb、cocotb-test、pytest、pytest-xdist、coverage 等）。 |
| `.github/workflows/surf_ci.yml` | CI 流水线，把 import + pytest + coverage 串成 test job。 |
| `tests/base/fifo/test_FifoSync.py` | 一个真实测试范例，示范方法论头、`PARAMETER_SWEEP`、`@cocotb.test`、pytest 包装函数的完整结构。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **ruckus 源缓存**——`import` 如何把全仓库源码展平成 `build/SRC_VHDL`，测试又如何读取它。
2. **`run_surf_vhdl_test`**——公共封装如何把一条 pytest 用例翻译成一次 GHDL 仿真。
3. **pytest 并行回归**——参数扫描如何展开、如何并行调度、构建目录如何隔离。

### 4.1 ruckus 源缓存：`import` 与 `build/SRC_VHDL`

#### 4.1.1 概念说明

cocotb 仿真一个 DUT 时，必须先告诉仿真器「这个 DUT 依赖哪些 VHDL 文件、各属于哪个库」。SURF 是一个庞大的共享库，一个 DUT 可能间接依赖几百个 `.vhd`，散落在 `axi/`、`base/`、`protocols/` 等子树、还夹着按 FPGA 家族分流（`getFpgaArch`）的 PHY 目录。如果让每个测试自己重新跑一遍 ruckus 的目录遍历，既慢又容易出错。

SURF 的解法是把「源码收集」和「仿真运行」**解耦**：

- **源码收集**只做一次：`make MODULES=$PWD import` 让 ruckus 跑一遍全部 `ruckus.tcl`，把所有要进构建的源文件**展平**（flatten）到一个固定缓存目录 `build/SRC_VHDL/`，按 VHDL 库（`surf`、`ruckus`）分子目录摆放。
- **仿真运行**反复读取这份缓存：每个测试只需从 `build/SRC_VHDL/surf/` 取出已经展平好的文件清单，交给 GHDL 编译。

这样，源码树里新增/删除文件后只需重跑一次 `import`；只要源清单没变，多次 `pytest` 都复用同一份缓存。这正是 `tests/README.md` 里「Run `make ... import` when the imported HDL source cache is missing or stale」的含义。

#### 4.1.2 核心流程

`import` 到仿真运行的流程可以这样描述：

```
make MODULES=$PWD import
        │
        ▼
ruckus 读 ruckus.tcl → loadRuckusTcl 遍历七大子树
        │              （core/ 无条件加载，家族 PHY 目录用 getFpgaArch 守卫）
        ▼
展平所有 .vhd 到 build/SRC_VHDL/{surf,ruckus}/  （按库分目录，每库一个文件列表）
        │
        ▼
pytest 启动 → 每个用例调用 run_surf_vhdl_test(...)
        │
        ▼
build_vhdl_sources() 读取 build/SRC_VHDL/{surf,ruckus}/ 的文件清单
        │
        ▼
交给 cocotb_test.simulator.run(...) → GHDL 编译 + elaborate + 仿真
```

注意 `MODULES` 的语义：根 `Makefile` 默认把 `MODULES` 指向「上一级目录」（`$(abspath $(PWD)/../)`），因为 SURF 通常作为子模块被别的工程嵌入。但当我们在 SURF 仓库**自身**里跑回归时，要显式 `make MODULES=$PWD ...`，让 ruckus 把当前目录当作顶层。CI 里所有命令都带着 `MODULES=$PWD`，就是这个原因。

#### 4.1.3 源码精读

先看根 `Makefile` 如何定义 `MODULES` 与 ruckus 目录，并 `include` 外部 makefile 库：

[Makefile:L14-L24](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/Makefile#L14-L24) — 设定 `MODULES`（默认指向上一级目录）、`RUCKUS_DIR`、`TOP_DIR`/`PROJ_DIR`/`OUT_DIR`，并设 `OVERRIDE_SUBMODULE_LOCKS=1`（SURF 在自身仓库里要绕过子模块版本锁）。`analysis` 与 `import` 两个目标并不在这个文件里，而是来自下一行的 `include`。

[Makefile:L42-L43](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/Makefile#L42-L43) — `include $(MODULES)/ruckus/system_ghdl.mk` 把外部 ruckus 子模块的 makefile 库拉进来，由它提供 `analysis`（GHDL 语法分析）和 `import`（生成源缓存）两个目标。这就是 u1-l2 强调的「根 Makefile 极短，只设参数后转交外部库」。

再看测试侧如何消费这份缓存。`regression_utils.py` 用模块级常量把所有「仓库相对路径」集中在一处：

[tests/common/regression_utils.py:L25-L27](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/common/regression_utils.py#L25-L27) — 定义 `REPO_ROOT`（本文件往上两级）、`TESTS_ROOT`、以及关键的 `BUILD_SRC_ROOT = REPO_ROOT / "build" / "SRC_VHDL"`。这就是 import 缓存的根。

真正读取缓存的函数是 `build_vhdl_sources`：

[tests/common/regression_utils.py:L160-L172](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/common/regression_utils.py#L160-L172) — 它检查 `build/SRC_VHDL/surf` 与 `build/SRC_VHDL/ruckus` 两个目录是否存在；若不存在就抛 `FileNotFoundError`，错误信息直接提示 `Run "make MODULES=\"$PWD\" import" first.`。存在则把每个库目录下的文件**排序后**列成清单返回（排序保证多次构建的源顺序稳定、编译可复现）。

CI 里 test job 正是按「先 import 再 pytest」的顺序执行的：

[.github/workflows/surf_ci.yml:L107-L110](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/.github/workflows/surf_ci.yml#L107-L110) — CI 的 `Parallel Regression Tests` 步骤先 `make MODULES=$PWD import` 生成缓存，紧接着跑 `python -m pytest --cov -v -n auto --dist=worksteal tests/axi tests/base tests/dsp tests/protocols`。对照之下，lint job 用的是 `make MODULES=$PWD analysis`（[surf_ci.yml:L76-L78](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/.github/workflows/surf_ci.yml#L76-L78)），两条线由此分明。

> 说明：`import` 目标的具体实现来自 ruckus 子模块的 `system_ghdl.mk`，本仓库以 git 子模块形式引用它（`ruckus.tcl` 里 `SubmoduleCheck {ruckus} {4.9.0}` 锁定版本）。本讲解读的 reader 检出里该子模块未展开，故 `import` 的内部行为以 CI 用法与 `build_vhdl_sources` 的期望目录结构为据。

#### 4.1.4 代码实践

**实践目标**：亲手生成源缓存，并验证测试侧确实在读它。

**操作步骤**：

1. 在仓库根目录执行（注意必须带 `MODULES=$PWD`）：

   ```bash
   make MODULES=$PWD import
   ```

2. 查看生成的缓存结构：

   ```bash
   ls build/SRC_VHDL/
   ls build/SRC_VHDL/surf/ | head
   ```

3. 故意把缓存挪走，再跑一个测试，观察报错：

   ```bash
   mv build build_moved
   ./.venv/bin/python -m pytest -q tests/base/fifo/test_FifoSync.py 2>&1 | head -40
   ```

**需要观察的现象**：

- 第 2 步应看到 `build/SRC_VHDL/` 下有 `surf`、`ruckus` 两个子目录，`surf/` 里是大量展平后的 `.vhd` 文件。
- 第 3 步 pytest 应失败，并打印 `build_vhdl_sources` 抛出的 `FileNotFoundError: Missing imported HDL sources. Run "make MODULES=\"$PWD\" import" first.`

**预期结果**：把缓存挪回来（`mv build_moved build`）后测试恢复正常。若环境里 ruckus 子模块未展开，`import` 步骤会失败——此时记下报错涉及的 ruckus 路径，标注「待本地验证（需先克隆 ruckus 子模块）」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SURF 要把「源码收集」和「仿真运行」拆成 `import` 和 `pytest` 两步，而不是让每个测试自己收集源码？

**参考答案**：SURF 源码量大、依赖 ruckus 的目录遍历与家族分流，重复收集既慢又不稳定；拆开后 `import` 只跑一次、产出稳定的展平清单（`build/SRC_VHDL`），所有测试复用同一份缓存，既加速又保证源顺序可复现。

**练习 2**：`build_vhdl_sources` 返回前对文件做了 `sorted()`，这个排序有意义吗？

**参考答案**：有。文件系统对目录的列举顺序不保证稳定，排序后同一份源清单在不同机器、不同次运行里顺序一致，使 GHDL 的编译/elaborate 顺序可复现，便于排查「换个环境就挂」的偶发问题。

---

### 4.2 `run_surf_vhdl_test`：把 pytest 用例翻译成一次 GHDL 仿真

#### 4.2.1 概念说明

`run_surf_vhdl_test` 是整个回归栈的「翻译核心」。cocotb 原生的工作方式是：仿真器先启动、再加载 Python 测试模块；而 SURF 的测试被 pytest 驱动，方向相反。`cocotb-test` 库的 `cocotb_test.simulator.run(...)` 负责把方向「倒过来」——由 Python 启动 GHDL、把指定源编译、elaborate 出顶层实体、再加载指定的 cocotb 测试模块去驱动它。

`run_surf_vhdl_test` 在这之上做了一层 SURF 专属封装，替每个测试处理四件重复琐事：

1. 从测试文件路径推算出 cocotb 要 `import` 的 **Python 模块名**。
2. 把 import 缓存里的源清单与测试自带的额外源（如薄封装 wrapper）**合并**。
3. 为每个参数组合生成**互不冲突**的仿真构建目录。
4. 给 GHDL 配上与根 Makefile 一致的**编译参数**（`--std=08` 等）。

这样，子系统测试只需写一个极简的 pytest 包装函数，调用 `run_surf_vhdl_test(...)` 即可。

#### 4.2.2 核心流程

一次 `run_surf_vhdl_test` 调用的内部展开：

```
run_surf_vhdl_test(test_file, toplevel, parameters, extra_env, ...)
   │
   ├─ _module_name_from_test_file(test_file)
   │      → 把 tests/base/fifo/test_FifoSync.py 转成 Python 模块路径 "tests.base.fifo.test_FifoSync"
   │
   ├─ build_vhdl_sources()          → 取 build/SRC_VHDL 的 surf/ruckus 清单
   ├─ merge_vhdl_sources(...)       → 追加 extra_vhdl_sources（测试自带的 wrapper）
   │
   ├─ _sim_build_path(test_file, parameters)
   │      → 生成 tests/sim_build/base/fifo/test_FifoSync.<k=v,k=v> 的参数专属目录
   │
   ├─ simulator_env = extra_env 或 parameters 的字符串化   → 作为仿真期环境变量
   │
   └─ cocotb_test.simulator.run(
          toplevel="surf.fifosync",        # VHDL 顶层（库.实体）
          module="tests.base.fifo.test_FifoSync",  # 含 @cocotb.test 的 Python 模块
          toplevel_lang="vhdl",
          vhdl_sources=<合并后的清单>,
          parameters=<HDL 泛型>,
          sim_build=<参数专属目录>,
          extra_env=<仿真期环境变量>,
          simulator="ghdl",
          vhdl_compile_args=COMMON_VHDL_COMPILE_ARGS,
      )
```

有两处设计值得注意：

- **HDL 泛型与环境变量的分工**：一个参数组合的字典里，只有键名以 `_G` 结尾的（如 `DATA_WIDTH_G`）才是真正的 VHDL 泛型，会传给 `parameters`；其余键（如 `CHECK_FULL_EMPTY`、`CLK_PERIOD_NS`）是 Python 侧的开关，通过 `extra_env` 变成环境变量，由测试里的 `env_flag(...)` 读回。`hdl_parameters_from(parameters)` 这个小函数就是干这个过滤的。
- **构建目录按参数隔离**：`_sim_build_path` 把参数拼进目录名（如 `test_FifoSync.DATA_WIDTH_G=16,ADDR_WIDTH_G=4,...`），让并行跑的多个参数组合各自编译、互不踩踏。

#### 4.2.3 源码精读

`run_surf_vhdl_test` 的完整签名与实现：

[tests/common/regression_utils.py:L213-L242](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/common/regression_utils.py#L213-L242) — 这是核心封装。注意三处：①它把 `extra_env`（若有）字符串化后既作为仿真期环境变量、又作为 `sim_build` 目录命名的依据（`sim_build_parameters`）；②`vhdl_sources` 由 `merge_vhdl_sources(build_vhdl_sources(), extra_vhdl_sources)` 提供，即「import 缓存 + 测试自带源」；③`simulator="ghdl"`、`vhdl_compile_args=COMMON_VHDL_COMPILE_ARGS` 把 GHDL 与编译参数钉死。

模块名推算函数（cocotb 要的是 Python 导入路径，不是文件系统路径）：

[tests/common/regression_utils.py:L191-L194](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/common/regression_utils.py#L191-L194) — 把测试文件相对 `REPO_ROOT` 的路径去掉扩展名、用 `.` 连接，例如 `tests/base/fifo/test_FifoSync.py` → `tests.base.fifo.test_FifoSync`。这要求 `tests/` 及各子目录都有 `__init__.py`（实际确实如此），否则 Python 无法按包导入。

源合并函数：

[tests/common/regression_utils.py:L175-L188](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/common/regression_utils.py#L175-L188) — 把 import 缓存里已编译的 SURF 库作为基线，再把测试自带的额外源（一般是把记录型端口扁平化的 wrapper）**追加在后面**。注释点明：追加顺序保证 wrapper 能引用前面已编译的真实 RTL。

参数专属构建目录：

[tests/common/regression_utils.py:L201-L210](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/common/regression_utils.py#L201-L210) — 无参数时目录就是 `tests/sim_build/<子系统>/<test_stem>`；有参数时把参数拼成后缀（`stem.key=value,...`），让并行 pytest 进程互不覆盖对方的编译产物。

编译参数与根 Makefile 的呼应：

[tests/common/regression_utils.py:L29-L34](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/common/regression_utils.py#L29-L34) 与 [tests/common/regression_utils.py:L65-L69](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/common/regression_utils.py#L65-L69) — `BASE_GHDL_COMPILE_ARGS` 用 `--std=08 -fsynopsys -frelaxed-rules -fexplicit`，这与根 Makefile 的 `GHDL_BASE_FLAGS`（[Makefile:L30-L35](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/Makefile#L30-L35)，`--std=08 --ieee=synopsys ...`）是同一组语义、两种写法（GHDL 命令行 `--ieee=synopsys` 与 `-fsynopsys` 等价）。`COMMON_VHDL_COMPILE_ARGS` 还动态剔除 `elaboration/hide/specs` 等可选警告（`_optional_ghdl_warning_flags`，[L60-L62](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/common/regression_utils.py#L60-L62)），再补 `-O2`。

最后看一个真实测试如何调用它，形成闭环：

[tests/base/fifo/test_FifoSync.py:L457-L464](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_FifoSync.py#L457-L464) — pytest 包装函数 `test_FifoSync` 用 `@pytest.mark.parametrize("parameters", PARAMETER_SWEEP)` 对每个参数组合调用 `run_surf_vhdl_test`，其中 `parameters=hdl_parameters_from(parameters)` 只把 `_G` 后缀的键作为 HDL 泛型，`extra_env=parameters` 则把整个字典（含 Python 开关）作为环境变量。`toplevel="surf.fifosync"` 指向一个 checked-in 的扁平化 wrapper 实体。

`hdl_parameters_from` 与 `parameter_case` 这对辅助工具：

[tests/common/regression_utils.py:L148-L157](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/common/regression_utils.py#L148-L157) — `parameter_case(case_id, **parameters)` 包一层 `pytest.param(..., id=case_id)`，让参数组合拥有可读的 pytest ID；`hdl_parameters_from` 只保留以 `_G` 结尾的键。这正是 `tests/README.md`「Pass only HDL generics as `parameters`」规则的代码实现。

#### 4.2.4 代码实践

**实践目标**：读一条真实测试，跟踪它的参数如何分别流向「HDL 泛型」和「环境变量」。

**操作步骤**：

1. 打开 `tests/base/fifo/test_FifoSync.py`，找到 `PARAMETER_SWEEP` 里名为 `fwft_threshold_midpoint` 的用例（约 [L405-L420](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_FifoSync.py#L405-L420)）。
2. 把这个字典的键分成两组：以 `_G` 结尾的、不以 `_G` 结尾的。
3. 在 `run_surf_vhdl_test` 调用处（[L459-L463](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_FifoSync.py#L459-L463)）确认：前者经 `hdl_parameters_from` 进 `parameters`，后者随 `extra_env=parameters` 进环境变量。
4. 在 TB 类里搜索 `env_flag("CHECK_THRESHOLD_FLAGS", ...)`（约 [L190](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_FifoSync.py#L190)）与 `env_flag("FWFT_EN_G", ...)`（约 [L44](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_FifoSync.py#L44)），观察 Python 侧如何把环境变量读回成行为开关。

**需要观察的现象**：

- `DATA_WIDTH_G`/`ADDR_WIDTH_G`/`FWFT_EN_G`/`MEMORY_TYPE_G` 等会作为 VHDL 泛型传给 `surf.fifosync`。
- `CHECK_THRESHOLD_FLAGS`/`CHECK_FULL_EMPTY`/`CLK_PERIOD_NS`/`RST_ACTIVE_HIGH` 不进泛型，而是经 `env_flag`/`os.environ` 在 Python 里读取，控制是否跑某段断言、时钟周期等。

**预期结果**：你能画一张表，把这个用例的每个键标成「HDL 泛型」或「Python 环境变量」并写出它的用途。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `toplevel="surf.fifosync"` 而不是直接 `"FifoSync"`？这个 `surf.` 前缀从哪来？

**参考答案**：`cocotb_test` 用「库名.实体名」定位 VHDL 顶层。`surf.` 表示该实体编译进 `surf` 库（源缓存里 `build/SRC_VHDL/surf/` 的文件都进 `surf` 库，见 `build_vhdl_sources` 返回的字典键）。`fifosync` 是一个 checked-in 的扁平化 wrapper 实体（把 FifoSync 的记录型端口拆成仿真器友好的扁平端口），而非原始 RTL。

**练习 2**：`extra_env` 同时被用作「仿真期环境变量」和「构建目录命名依据」，这样做有什么好处和风险？

**参考答案**：好处是构建目录天然按「会影响仿真的全部变量」隔离，不同参数组合不会互相覆盖编译产物；风险是若某变量取值很长或含特殊字符，目录名会变得脆弱——`tests/README.md` 因此建议在用例元数据较多时改用 `sim_build_key` 显式指定一个短而稳定的构建目录名。

---

### 4.3 pytest 并行回归：参数扫描与 work-stealing 调度

#### 4.3.1 概念说明

前两节解决了「单条用例如何变成一次仿真」。但 SURF 的回归是**成百上千次**仿真：每个测试文件有十几个参数组合，每个组合都是一次独立的 GHDL 编译 + 仿真。串行跑会非常慢。SURF 用 `pytest-xdist` 做并行：

- **参数扫描**：`PARAMETER_SWEEP` 列表 + `@pytest.mark.parametrize` 让 pytest 把每个参数组合展开成一个独立的 test item，每个 item 都是一次完整仿真。
- **并行调度**：`pytest -n auto` 启动「每 CPU 一个 worker」，`--dist=worksteal` 让空闲 worker 主动从忙 worker 那里「偷」任务，实现负载均衡。

之所以需要 work-stealing 而非简单的轮询（round-robin）分发，是因为不同仿真耗时差异很大（编译量大 vs 小、激励长 vs 短），简单均分会让某些 worker 早早空闲、另一些还在苦撑。work-stealing 把这种不均衡「抹平」。

#### 4.3.2 核心流程

从 `PARAMETER_SWEEP` 到并行仿真的展开过程：

```
PARAMETER_SWEEP = [parameter_case("case_a", ...), parameter_case("case_b", ...), ...]
        │
        ▼  @pytest.mark.parametrize("parameters", PARAMETER_SWEEP)
pytest 发现 N 个 test item：
   tests/base/fifo/test_FifoSync.py::test_FifoSync[case_a]
   tests/base/fifo/test_FifoSync.py::test_FifoSync[case_b]
   ...
        │
        ▼  pytest -n auto --dist=worksteal
xdist 把 test item 分发到多个 worker 进程（work-stealing 动态均衡）
        │
        ▼  每个 worker 对自己分到的 item 执行：
run_surf_vhdl_test(...) → cocotb_test.simulator.run(...)
   ├─ 在 _sim_build_path 给出的「参数专属目录」里编译
   ├─ GHDL elaborate 顶层 surf.fifosync
   └─ 加载 Python 测试模块，跑 @cocotb.test 协程，收集断言
        │
        ▼
pytest 汇总各 worker 的通过/失败，配合 --cov 收覆盖率
```

关键点：每个参数组合的编译产物落在**各自的** `tests/sim_build/.../<stem>.<k=v,...>` 目录（见 4.2 的 `_sim_build_path`），这是并行安全的前提——若所有参数共用一个目录，并行 worker 会互相覆盖编译文件而崩溃。

#### 4.3.3 源码精读

依赖锁定在 `pip_requirements.txt`：

[pip_requirements.txt:L1-L11](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/pip_requirements.txt#L1-L11) — 列出 `cocotb`、`cocotb-test`、`pytest`、`pytest-xdist`（并行）、`pytest-cov`/`coverage`/`codecov`（覆盖率）、`cocotbext-axi`（AXI 仿真帮手）、`flake8`/`vsg`/`cpplint`（lint）。这套依赖正是 lint 与 test 两条 CI job 的共同基础。

pytest 自身配置极简：

[pytest.ini:L1-L2](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/pytest.ini#L1-L2) — 仅声明 `norecursedirs = tests/legacy`，即让 pytest 跳过已被子系统测试取代的旧平铺测试（与 `tests/README.md` 的 Layout 规则呼应：被取代的旧测试迁入 `tests/legacy/`）。

CI 的并行回归命令就是本节的范例：

[.github/workflows/surf_ci.yml:L107-L110](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/.github/workflows/surf_ci.yml#L107-L110) — `python -m pytest --cov -v -n auto --dist=worksteal tests/axi tests/base tests/dsp tests/protocols`：`--cov` 开覆盖率、`-v` 逐用例打印、`-n auto` 每 CPU 一个 worker、`--dist=worksteal` 工作偷取。紧接着的 `coverage report -m`（[L113-L115](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/.github/workflows/surf_ci.yml#L113-L115)）输出逐行覆盖率报告。

`tests/README.md` 给出的本地等价命令与调试建议：

[tests/README.md:L131-L141](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/README.md#L131-L141) — 推荐 `./.venv/bin/python -m pytest -n auto --dist=worksteal -q tests/<subsystem>`；并指出「用 `-n 0` 做聚焦调试」——即关掉并行、串行跑，这样仿真器日志不会被多进程打乱，便于定位单个失败用例。

参数扫描的最佳实践写在风格指南里：

[tests/README.md:L67-L82](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/README.md#L67-L82) — 强调「精选矩阵优先于宽泛笛卡尔积」：好的扫描应覆盖默认路径、一两个有意义的泛型分支、复位极性/异步复位、窄/宽数据通路、反压或流水。扫描 ID 要短而有意义，因为它们会成为 pytest ID 与构建目录名。

#### 4.3.4 代码实践

**实践目标**：对比并行与串行运行，体会 work-stealing 的作用与 `-n 0` 的调试价值。

**操作步骤**：

1. 先确保源缓存存在：`make MODULES=$PWD import`。
2. 并行跑 fifo 子系统（若可用）：

   ```bash
   ./.venv/bin/python -m pytest -n auto --dist=worksteal -q tests/base/fifo
   ```

3. 串行跑同一个子系统，对比日志形态：

   ```bash
   ./.venv/bin/python -m pytest -n 0 -v tests/base/fifo/test_FifoSync.py
   ```

4. 观察构建目录的隔离效果：

   ```bash
   ls tests/sim_build/base/fifo/ | head
   ```

**需要观察的现象**：

- 第 2 步：pytest 会报告 worker 数与分发策略，总耗时显著短于串行；`-q` 下输出精简。
- 第 3 步：`-n 0 -v` 逐用例列出 `test_FifoSync[block_fwft_baseline]`、`test_FifoSync[distributed_fwft]` 等 ID，日志顺序确定、可读，便于定位失败。
- 第 4 步：`test_FifoSync.*` 下出现多个以参数后缀命名的子目录，证明每个参数组合各有独立编译目录。

**预期结果**：并行与串行的通过/失败集合一致。若某用例只在并行下偶发失败，应怀疑构建目录隔离或仿真器残留进程问题（`tests/README.md` 提醒：跑完一次仿真后要检查是否有残留的仿真器子进程）。若本地未装 GHDL/cocotb，第 2、3 步标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`--dist=worksteal` 与 pytest-xdist 默认的 `--dist=load`（按测试时长预估均分）相比，为什么更适合 SURF 回归？

**参考答案**：SURF 各仿真的实际耗时差异极大（不同参数组合的编译量、激励长度、是否带反压都不同），且难以预先估计。work-stealing 让空闲 worker 主动偷任务，能动态抹平这种不均衡，比静态均分更接近理想的最短总时长。

**练习 2**：如果两个参数组合共用同一个 `sim_build` 目录，并行运行时会发生什么？SURF 用什么机制避免？

**参考答案**：两个 worker 会同时读写同一组 GHDL 编译/elaborate 文件，导致文件冲突或互相覆盖，仿真随机崩溃。SURF 用 `_sim_build_path` 把参数组合拼进目录名（`<stem>.<key=value,...>`），保证每个组合有独立目录，从而并行安全。

## 5. 综合实践

把三个最小模块串起来，完成一次「端到端」的子系统回归：

1. **准备源缓存**（4.1）：`make MODULES=$PWD import`，确认 `build/SRC_VHDL/{surf,ruckus}` 已生成。
2. **跑子系统回归**（4.3）：

   ```bash
   ./.venv/bin/python -m pytest -q tests/base/fifo
   ```

   记录：总用例数、通过/失败数、总耗时、worker 数。
3. **单点调试**（4.3）：挑一个失败的（或最简单的）用例，用 `-n 0 -v` 串行重跑，阅读完整仿真日志。
4. **追踪翻译链**（4.2）：对任意一个用例 ID（如 `test_FifoSync[block_fwft_baseline]`），手动还原它的参数如何被 `_module_name_from_test_file`、`merge_vhdl_sources`、`_sim_build_path`、`hdl_parameters_from` 处理，并写出它对应的 `toplevel`、Python 模块名、构建目录名。

**验收标准**：你能对着一条 pytest 用例 ID，画出从「pytest 发现 → `run_surf_vhdl_test` → `cocotb_test.simulator.run` → GHDL 编译/仿真 → cocotb 协程断言」的完整数据流，并指明源缓存、构建目录、环境变量在其中的位置。

> 若本地环境缺少 ruckus 子模块、GHDL 或 cocotb，以上命令可能无法执行——此时把第 2、3 步降级为「源码阅读型实践」：阅读 `regression_utils.py` 与 `test_FifoSync.py`，在纸上完成第 4 步的追踪，并标注「待本地验证」。

## 6. 本讲小结

- SURF 回归栈是四块拼图：**ruckus** 收集源、**GHDL** 仿真、**cocotb** 注入激励与断言、**pytest** 发现与并行调度，中间靠 **cocotb-test** 把「pytest → GHDL」的方向倒过来。
- `make MODULES=$PWD import` 让 ruckus 把全仓库源码展平成 `build/SRC_VHDL/{surf,ruckus}` 缓存；`build_vhdl_sources()` 读取它，缓存缺失时报错并提示先 import。源码收集与仿真运行由此解耦。
- `run_surf_vhdl_test` 是翻译核心：它推算 Python 模块名、合并源清单、生成参数专属构建目录、钉死 GHDL 编译参数，最终调用 `cocotb_test.simulator.run` 启动一次仿真。
- 参数扫描里，以 `_G` 结尾的键经 `hdl_parameters_from` 进 VHDL 泛型，其余键经 `extra_env` 进环境变量、由 `env_flag` 等读回——这是「HDL 泛型 vs Python 开关」的分工。
- `pytest -n auto --dist=worksteal` 用工作偷取并行调度数百次仿真；每个参数组合的编译产物落在 `_sim_build_path` 给出的独立目录，是并行安全的前提；`-n 0` 用于聚焦调试。
- CI 里 lint 线用 `make ... analysis` 做语法检查，test 线用 `make ... import` + pytest 做回归，两条线共用同一套 Makefile/GHDL/ruckus 基础设施。

## 7. 下一步学习建议

- 下一篇 **u9-l2「编写一个 cocotb 回归测试」** 会钻进测试文件内部，讲解 `Test methodology` 头、TB 类、`@cocotb.test` 协程、`PARAMETER_SWEEP` 与 pytest 包装函数的标准结构，并以 `tests/base/fifo/test_Fifo.py` 为范例带你写一个新测试。
- 想深入测试辅助原语，可先读 `tests/axi/utils.py`（AXI-Lite 读写、`wait_sampled_ready`、`start_lockstep_clocks`），它会在 **u9-l3** 详讲。
- 想理解「源码是怎么被收集进缓存的」，可回看 u1-l2 与顶层 `ruckus.tcl`（`loadRuckusTcl` 如何递归加载七大子树），并对比 `analysis` 与 `import` 两个目标的异同。
