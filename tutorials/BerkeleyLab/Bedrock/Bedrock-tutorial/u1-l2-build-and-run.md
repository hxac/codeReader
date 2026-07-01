# 构建与运行：工具链与 selftest

## 1. 本讲目标

上一讲（[u1-l1](u1-l1-project-overview.md)）我们认识了 Bedrock 是什么、它由哪些子系统组成。本讲解决一个非常实际的问题：**在一台干净的机器上，怎么把 Bedrock 跑起来，并验证它「能工作」？**

学完本讲，你应当能够：

1. 说出 Bedrock 的必需依赖和推荐依赖分别有哪些，以及它们的用途。
2. 解释 `selftest.sh` 的设计意图：它如何把 GitLab CI 上的测试阶段在本地一键复现。
3. 看懂 `selftest.sh` 里每一行 `make -C <子目录> ...` 在调用哪个子系统的测试入口。
4. 在自己的机器上运行 `sh selftest.sh`，并能区分「PASS」「被跳过」「缺工具」三种结果各自的原因。

本讲只触及两个最小模块：**依赖清单 `dependencies.txt`** 与**测试编排脚本 `selftest.sh`**。它们是后续所有「动手跑测试」讲义的地基。

---

## 2. 前置知识

在进入源码之前，先用大白话澄清几个概念。

- **工具链（toolchain）**：把源代码变成可运行产物的整套工具。Bedrock 主要用三类工具：
  - **GNU Make**：一个 1976 年诞生的「依赖驱动」构建工具。你写一个 `Makefile`，声明「目标 A 依赖 B、C，生成命令是 …」，Make 就会在 B、C 变化时自动重建 A。Bedrock 全仓库的测试、仿真、生成都跑在 Make 之上。
  - **Icarus Verilog（命令名 `iverilog` / `vvp`）**：一个开源的 Verilog 仿真器。`iverilog` 把 `.v` 编译成可执行文件，`vvp` 负责运行它、产生仿真结果。
  - **Python 3**：Bedrock 大量用 Python 做代码生成、结果校验和 cocotb 仿真驱动。
- **CI（Continuous Integration，持续集成）**：每次提交代码，服务器自动跑一遍全部测试，确保没有把主干弄坏。Bedrock 用的是 GitLab CI。
- **CDC（Clock Domain Crossing，时钟域跨越）**：本讲只要知道「有些测试是专门检查 CDC 正确性的」即可，原理留到 u4-l1 讲。
- **flake8**：一个 Python 代码风格检查器。Bedrock 把它也纳入了 selftest，确保所有 `.py` 文件风格一致。
- **PASS / skip**：测试结果用语。PASS 表示通过；skip（跳过）通常因为缺少某个可选工具，**不算失败**，理解这一点对读懂 selftest 输出很关键。

> 关键直觉：Bedrock 的设计哲学是「命令行批处理优先」。selftest.sh 不是某个神秘魔法，它就是「按字母顺序，挨个子目录地 `make` 一遍」。理解了这一点，本讲的源码就读完一半了。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲如何使用 |
| --- | --- | --- |
| `dependencies.txt` | 仓库根目录的依赖清单，按「必需 / 可选」分组，并给出 Debian、Fedora 的安装命令。 | 讲清「装什么」 |
| `selftest.sh` | 仓库根目录的 POSIX shell 脚本，按 GitLab 流水线页面的字母顺序，逐个子系统调用 `make`。 | 讲清「怎么跑、跑的是什么」 |
| `README.md` | 顶层说明，其中 `Dependencies` 一节是依赖的人话版概览。 | 交叉印证依赖分类 |
| `cordic/Makefile` | cordic 子系统的 Makefile，定义了 `all` / `clean` 等目标。 | 作为「单个子系统测试入口」的具体例子 |

永久链接的 base 为：

```
https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/
```

下文每个引用都拼接相对路径与行号锚点。

---

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 依赖清单：装哪些工具（dependencies.txt）**
- **4.2 selftest.sh：在本地一键复现 CI 测试**

### 4.1 依赖清单：装哪些工具

#### 4.1.1 概念说明

要在本地「跑通」Bedrock，第一步是装对工具。Bedrock 把工具分成两档：

- **必需（Required）**：缺了任何一个，绝大多数测试都跑不起来。这是地基。
- **可选（optional / Recommended）**：缺了它们，仓库里**大部分测试仍能 PASS**，只有少数依赖该工具的测试会被跳过（skip）。

这种「必需 + 可选」的分层，是为了降低上手门槛——你不必先把所有重型工具（如 Xilinx Vivado）都装齐，才能开始学习。

#### 4.1.2 核心流程

依赖准备的决策流程可以画成：

```
 你拿到一台干净机器
        │
        ▼
 装 [Required]：make / iverilog / python3-numpy / flake8
        │
        ▼  现在能跑 selftest 的「主菜」了吗？
        │
   ┌────┴─────────────────┐
   是                      否（缺必需工具）→ selftest 在打印版本处就会报错退出
   │
   ▼
 按 [optional] 按需补装：verilator / yosys / scipy / matplotlib / cocotb
        │
        ▼
 运行 sh selftest.sh，缺哪个可选工具，对应测试就 skip，其余 PASS
```

注意一个细节：`numpy` 被列为**必需**，而 `scipy` / `matplotlib` 是**可选**。这是因为许多 Python 校验脚本（如 cordic 的 `cordic_check.py`）只要 numpy 就能算，而画图（`.png`）和高级信号处理才需要后两者。

#### 4.1.3 源码精读

依赖清单的主文件是 [dependencies.txt](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dependencies.txt#L1-L53)，它用 `[Required]` / `[optional]` 两个方括号小节来分组：

- **必需依赖小节**：[dependencies.txt:1-9](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dependencies.txt#L1-L9) 列出 `make`、`iverilog`、`python3-numpy`、`python3-pip`、`flake8`，并强调 python2 已过生命周期，统一用 python3。
- **可选依赖小节**：[dependencies.txt:12-17](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dependencies.txt#L12-L17) 列出 `Verilator`、`Yosys`、`python3-scipy`、`python3-matplotlib`。
- **Debian 一键安装摘要**：[dependencies.txt:19-24](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dependencies.txt#L19-L24) 给出了在 Debian Bookworm / Trixie 上经过充分测试的两条命令——`apt-get install …` 装系统包，`pip3 install cocotb cocotb-bus` 装 Python 包。这是最省心的安装路径。
- **Fedora 的坑**：[dependencies.txt:26-44](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dependencies.txt#L26-L44) 提醒 Fedora 上存在 iverilog-vpi 配置 bug，建议直接从源码编译 iverilog 10.2，并附带了带校验和的下载步骤。

README 里有一份更口语化、更精简的依赖概览，和 `dependencies.txt` 互为印证：

- [README.md:80-96](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/README.md#L80-L96) 把依赖分成 Required（GNU Make / iverilog / Python）和 Recommended（GTKWave / Xilinx Vivado / Verilator / YoSys），并指向 `dependencies.txt` 与 `Dockerfile.bedrock_testing_base_trixie` 作为「完整清单」。

> 一个有用的对照：README 的「Recommended」比 dependencies.txt 的「optional」多了 **GTKWave**（看波形）和 **Xilinx Vivado**（综合上板）。这两个对「跑测试」非必需，但对「看波形」「上 FPGA」很关键。

#### 4.1.4 代码实践

1. **实践目标**：搞清楚自己的机器缺哪些必需依赖。
2. **操作步骤**：在终端逐条检查（这些命令只读、安全）：

   ```sh
   make --version          # GNU Make
   iverilog -V             # Icarus Verilog
   python3 --version       # Python 3
   python3 -c 'import numpy; print(numpy.__version__)'   # numpy
   flake8 --version        # Python 风格检查
   ```
3. **需要观察的现象**：每条都应打印出版本号。如果某条报 `command not found` 或 `ModuleNotFoundError`，说明该必需依赖缺失。
4. **预期结果**：五条全部有版本输出 = 必需依赖齐全，可以进入 4.2 跑 selftest。
5. 若某条缺失，按 [dependencies.txt:22](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dependencies.txt#L22) 的 Debian 命令补装。**运行结果待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：为什么 `numpy` 是必需依赖，而 `scipy`、`matplotlib` 是可选？

> **参考答案**：大量 Python 校验脚本（例如核对 CORDIC 输出）只需 numpy 的数组运算即可完成数值比对；scipy 的信号处理函数和 matplotlib 的画图功能只在少数测试（如生成 `perf.png`、滤波器设计）用到。把它们设为可选，能让「只装最小集」的用户也能跑通绝大多数测试。

**练习 2**：在 Fedora 上 `yum install iverilog` 之后，为什么 selftest 可能仍然失败？该怎么办？

> **参考答案**：dependencies.txt 指出 Fedora 存在 iverilog-vpi 配置 bug（看似由 RedHat 引起），会导致用到 VPI 的测试出错。规避办法是卸载发行版版本，按 [dependencies.txt:35-44](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dependencies.txt#L35-L44) 从源码编译 iverilog 10.2 并校验 sha256。

---

### 4.2 selftest.sh：在本地一键复现 CI 测试

#### 4.2.1 概念说明

`selftest.sh` 是仓库根目录的一个 shell 脚本，它的定位用脚本头一句话就说清楚了：「主要覆盖 Bedrock GitLab CI 的 test 阶段，外加 flake8」。换句话说，**它是 CI 测试的「本地翻版」**：CI 在服务器上每次提交自动跑的那些测试，你在自己的工作站上敲一条命令就能复现，而且不必折腾 Docker。

理解它有两个关键点：

1. **它不是一个集中的「测试框架」**。它只是「按字母顺序，对每个子目录各跑一次 `make`」。真正干活的是各子目录自己的 Makefile（u2-l1 会详讲）。selftest 的价值在于「收集 + 排序 + 加上版本巡检」。
2. **缺可选工具 ≠ 失败**。脚本里凡是要用到 migen、RISC-V 工具链、SymbiYosys 的测试，都被显式地包在 `if [ "$1" = "more" ]` 里或被注释跳过。所以一份 selftest 输出里有 skip 是正常的。

#### 4.2.2 核心流程

`selftest.sh` 的执行结构分四大段：

```
① 环境巡检：打印 uname / gcc / python3 / numpy / cocotb / iverilog / verilator
            / yosys / tclsh / flake8 的版本（缺工具时这里最先暴露）
        │
        ▼
② unset DISPLAY  ← 防止任何测试意外弹出图形窗口
        │
        ▼
③ 主菜：按 GitLab 流水线页面的字母顺序，逐个子系统调用 make -C <dir> …
        例如 badger_cdc → badger_test → board_support_test → … → xilinx_test
        │
        ▼
④ 收尾：sleep 0.4; echo "selftest OK"   ← 只有前面全没出错才会打印这句
```

关于 ① 的设计：脚本建议用 `sh -ex selftest.sh` 运行（见 [selftest.sh:31-34](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L31-L34)）。`-e` 表示「任何命令失败立刻退出」，`-x` 表示「打印每条将要执行的命令」。配合 ① 的版本打印，**如果某个必需工具没装，脚本会在最早的时刻、在最显眼的位置停下来**，而不是让你面对后面一堆费解的 make 报错。

关于 ④：注意脚本**故意不在各步骤之间自动 `git clean -fdx`**（见 [selftest.sh:36](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L36) 的注释），因为自动清理太危险——会删掉你没提交的工作。各子目录的 Makefile 自己负责 `clean`。

#### 4.2.3 源码精读

**脚本头与定位**：[selftest.sh:1-7](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L1-L7) 说明它「覆盖 GitLab CI 的 test 阶段 + flake8」，并给出实测耗时（在 Ryzen 5 PRO 5650GE 上约 4 分钟）。

**运行环境与安装建议**：[selftest.sh:9-18](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L9-L18) 指出可在 Debian Bookworm / Trixie 上以非特权用户运行，并列出 `apt-get install …` 与 `pip install cocotb==2.0.1 cocotb-bus==0.3.0 leep==1.0.2` 等命令，还建议用 `venv --system-site-packages` 管理 Python 环境。

**「more」增强模式**：[selftest.sh:26-29](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L26-L29) 说明传一个参数 `more` 会启用一整套额外测试，需要 RISC-V 交叉编译链（`gcc-riscv64-unknown-elf`、`picolibc-riscv64-unknown-elf`）和 `migen==0.9.2`。

**环境巡检段**：[selftest.sh:38-56](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L38-L56) 逐个打印工具版本；其中 `more` 模式下还会额外检查 migen 与 `riscv64-unknown-elf-gcc`。

**关闭图形**：[selftest.sh:59](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L59) 的 `unset DISPLAY` 确保测试不会意外弹出窗口。

**主菜——按字母顺序的测试清单**：[selftest.sh:61-156](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L61-L156) 的注释明确说「下列测试按它们在 gitlab 流水线状态页上的（字母）顺序排列」。挑几条典型行来看：

- **badger_cdc**：[selftest.sh:64-65](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L64-L65) 跑 `make -C badger/tests hw_test_cdc.txt`，这是 badger 的形式化 CDC 检查。
- **cordic_test**：[selftest.sh:81-82](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L81-L82) 跑 `make -C cordic clean all`——这正是本讲实践任务里的备用命令。
- **dsp_test**：[selftest.sh:93-94](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L93-L94) 跑 `make -C dsp all checks`，其中 `checks` 是 dsp 子目录里一组 `*_check` 目标的聚合（u2-l1 详讲）。
- **localbus**：[selftest.sh:103-104](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L103-L104) 跑 `make -C localbus`，直接用默认目标。
- **xilinx_test**：[selftest.sh:152-153](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L152-L153) 跑 `make -C fpga_family/xilinx`，测试厂家原语包装。
- **flake8**：[selftest.sh:155-156](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L155-L156) 用 `find . -name "*.py" -exec flake8 {} +` 检查全仓库 Python 风格。

**被「more」门控的可选测试**：[selftest.sh:121-125](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L121-L125)（oscope，需要 migen）与 [selftest.sh:141-147](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L141-L147)（soc/picorv32，需要 RISC-V 工具链）都包在 `if [ "$1" = "more" ]` 里；后者注释还特别说明**形式化验证步骤被整个跳过**，因为依赖 SymbiYosys，而它不在 Debian 里、必须源码编译。

**收尾**：[selftest.sh:158-160](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L158-L160) 的 `echo "selftest OK"` 是「全绿」的标志——只有前面所有命令都成功，这句才会被执行并打印。

#### 4.2.4 代码实践

1. **实践目标**：在本地跑通（或至少跑通一个子系统的）测试，并学会区分 PASS / skip / 失败。
2. **操作步骤**（按由小到大的粒度选一个）：
   - **最小粒度**：先单独验证 cordic 子系统。在仓库根目录执行：

     ```sh
     make -C cordic clean all
     ```

     对照 [cordic/Makefile:7](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/Makefile#L7)，`all` 目标会依次构建 `cordic_ptor_check cordic_rtop_check cordic_bias_check cordic_fllw_check perf.png`，即四种 CORDIC 模式的回归校验加一张性能图。
   - **完整粒度**：在仓库根目录执行：

     ```sh
     sh -ex selftest.sh
     ```

     （加 `more` 启用增强测试：`sh -ex selftest.sh more`。）
3. **需要观察的现象**：
   - `-x` 会让 shell 先打印每条命令再执行，你能清楚看到「现在在跑哪个子系统的哪个 target」。
   - 每个子测试通常以打印 `PASS`（自检型 testbench 走 `$finish`）或 `cmp … && echo PASS`（与 golden 文件比对）结尾。
   - 若你没装 verilator / yosys / migen，相关行会报错或被 `more` 门控跳过——这是预期内的。
4. **预期结果**：必需依赖齐全时，主菜部分绝大多数子系统打印 PASS；用到可选工具的部分 skip；最后（在最小粒度下）看到四个 check 各自的 PASS 与 `perf.png` 生成，（在完整粒度下）最后一行打印 `selftest OK`。
5. **记录任务**：把结果整理成三列——`子系统 / 结果（PASS·skip·FAIL）/ 原因`。对每个 skip 写一句话解释是缺哪个工具（提示：对照 4.1 的可选清单）。**运行结果待本地验证。**

> 提示：如果你只想验证「环境是否装好」而不想等 4 分钟，重点看脚本**最开始**那段版本巡检（4.2.3 的环境巡检段）。它一旦报错退出，就是某个必需工具没装到位。

#### 4.2.5 小练习与答案

**练习 1**：为什么 selftest.sh 不在每两个子测试之间自动执行 `git clean -fdx`？

> **参考答案**：[selftest.sh:36](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L36) 的注释说「自动在步骤间 git clean 被认为太危险」。`git clean -fdx` 会删除所有未跟踪的文件，可能毁掉用户尚未提交的工作与生成物。各子目录的 Makefile 已自带受控的 `clean` 目标，由用户显式调用更安全。

**练习 2**：默认 `sh selftest.sh`（不带 `more`）会跑 soc/picorv32 的测试吗？为什么？

> **参考答案**：不会。[selftest.sh:141-147](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L141-L147) 把 `make -C soc/picorv32/test check` 包在 `if [ "$1" = "more" ]` 里，因为它需要 RISC-V 交叉编译工具链（Debian 里默认没有）。只有 `sh selftest.sh more` 才会跑。

**练习 3**：selftest.sh 最后的 `echo "selftest OK"` 在什么条件下才会打印？

> **参考答案**：只有脚本中此前所有命令都成功（退出码为 0）时才会打印。当你用 `sh -ex selftest.sh` 运行时，`-e` 让任何命令失败立即退出，于是只要某条 `make` 失败，脚本就会中途停止，根本到不了 [selftest.sh:160](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/selftest.sh#L160)。因此「看到 `selftest OK`」等价于「整套核心测试通过」。

---

## 5. 综合实践

把 4.1 和 4.2 串起来，完成下面这个贯穿性小任务：

**任务：制作一份属于你机器的「Bedrock 环境体检报告」。**

1. 运行 4.1.4 的五条版本检查命令，记录每个必需依赖的版本号或「缺失」。
2. 对照 [dependencies.txt:1-17](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dependencies.txt#L1-L17)，给每个可选依赖也标上「已装 / 缺失」。
3. 执行 `make -C cordic clean all`（最小粒度）。把输出里每个 `*_check` 是否 PASS 记下来。
4. 回答两个问题：
   - 你的环境能跑完整 `sh selftest.sh` 吗？如果不行，瓶颈是哪一个必需依赖？
   - 哪些子系统测试会因为缺可选工具而 skip？至少举出两个，并指出各自缺的是什么（提示：migen、RISC-V 工具链、SymbiYosys、verilator、yosys 都对应了 selftest 里被门控或注释跳过的某些测试）。

**产出**：一份不超过一页的 Markdown 表格 + 两段结论。这张表同时也是你后续学习每一讲「动手实践」前的环境基线——以后任何一讲让你跑某个 `make xxx_check` 失败时，先回来看这张表，往往就是某个可选工具没装。

> 说明：本实践不假定你能联网安装全部工具。即便只能装齐必需依赖，也完全可以完成「cordic 单子系统 PASS + 解释其余 skip 原因」的子目标。运行结果待本地验证。

---

## 6. 本讲小结

- Bedrock 把工具分成**必需**（make / iverilog / python3-numpy / python3-pip / flake8）与**可选**（verilator / yosys / scipy / matplotlib / cocotb 等），清单写在 [dependencies.txt](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dependencies.txt#L1-L53)。
- `selftest.sh` 是 GitLab CI test 阶段的**本地翻版**，本质是「按字母顺序逐个子目录地 `make`」，外加环境巡检和 flake8。
- 脚本建议用 `sh -ex selftest.sh` 运行：`-e` 让缺工具尽早暴露，`-x` 让每步可见。
- 缺可选工具会**跳过（skip）相应测试而非整体失败**；`more` 参数与若干注释门控了需要 migen / RISC-V 链 / SymbiYosys 的测试。
- 脚本故意不自动 `git clean`，`clean` 由各子目录 Makefile 负责；只有全程无错才会打印 `selftest OK`。
- 单个子系统的测试入口形如 `make -C cordic clean all`、`make -C dsp all checks`、`make -C localbus`——这套「`make -C <dir> [target]`」是后续每讲都会用到的统一手势。

---

## 7. 下一步学习建议

环境跑通之后，下一步是**理解 selftest 里那些 `make` 命令背后的机制**。建议：

1. 先读 [u1-l3](u1-l3-directory-structure.md)「目录结构与代码导航」，学会在大型 Verilog 库里快速定位模块、testbench、波形配置与 Makefile。
2. 再读 [u2-l1](u2-l1-make-hdl-testing.md)「基于 Make 的 HDL 仿真测试方法」，深入 `build-tools/top_rules.mk` 的模式规则（`%_tb` / `%_check` / `%.vcd`），搞清楚 `make -C cordic clean all` 里每一步到底调了什么 iverilog / vvp 命令。
3. 想直接看作者本人讲 Makefile，可阅读 [build-tools/makefile.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/makefile.md#L1-L246)，它是 u2-l1 的原始素材。

完成这三步后，你就能从「跑别人的 selftest」升级到「看懂并改写任意子系统的 Makefile 测试目标」。
