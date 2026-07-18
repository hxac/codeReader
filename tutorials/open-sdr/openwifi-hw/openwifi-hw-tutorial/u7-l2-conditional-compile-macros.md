# 条件编译与 Verilog 宏体系

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 openwifi-hw 为什么需要 `_pre_def.v` 这一层文件，它解决了什么问题。
- 复述「命令行参数 → Verilog `` `define ``」的两条生成路径：顶层工程的 `create_ip_repo.sh` 与单 IP 独立的 `create_vivado_proj.sh`。
- 理解「用户传 `ENABLE_DBG`，最终却变成 `XPU_ENABLE_DBG` / `TX_INTF_ENABLE_DBG`」这套**按 IP 名加前缀**的命名约定，以及它为什么是必须的。
- 区分四类条件编译宏：板卡派生宏、特性开关宏、用户调试宏、运行时配置宏，并知道每类宏分别从哪个脚本、哪个文件落地。
- 看懂 `SMALL_FPGA`、`SIDE_CH_LESS_BRAM`、`HAS_SIDE_CH`、`XPU_ENABLE_DBG` 等宏在真实 Verilog 里如何启用/裁剪代码块。
- 自己写出一条为所有 IP 启用 ILA/DEBUG 宏的 `create_ip_repo.sh` 命令，并指出每个 `` `define `` 写进了哪个文件。

本讲是**进阶/专家层**内容，承接 u1-l4（构建脚本链路）与 u2-l4（板级配置与时钟体系），把「构建期如何向 Verilog 注入编译期配置」这件事讲透。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

**第一，Verilog 本身缺少「编译期配置」的标准入口。** 像 C 语言可以用 `-DFOO=1` 从命令行注入宏，但 Vivado 综合 Verilog 时，最稳妥的跨文件、跨 IP 共享配置的方式，是写一个只含若干 `` `define `` 的小 `.v` 文件，再让需要它的源文件 `` `include `` 进来。openwifi-hw 把这个文件命名为 `<ip_name>_pre_def.v`（pre-define，预定义），每个 IP 一份。

**第二，openwifi 六个自研 IP 最终会塞进同一个 Vivado 工程。** 这带来一个约束：你不能给所有 IP 都用同一个叫 `pre_def.v` 的文件名却塞不同内容——同一工程里同名文件内容必须一致。openwifi 的解法是两套「命名空间隔离」：

1. 文件名按 IP 区分：`xpu_pre_def.v`、`tx_intf_pre_def.v`、……
2. 宏名按 IP 加前缀：用户传 `ENABLE_DBG`，到 `xpu` 里就变成 `XPU_ENABLE_DBG`。

**第三，`_pre_def.v` 不进 git。** 它是构建期由 Bash/Tcl 脚本现场生成的「配置快照」，内容随板卡（`BOARD_NAME`）、基带时钟（`NUM_CLK_PER_US`）和你传入的调试开关而变。所以它属于「生成物」而非「源码」——这也是为什么你看不到任何 `*_pre_def.v` 被 git 跟踪。

> 关键术语：`` `define `` / `` `ifdef `` / `` `include ``（Verilog 文本预处理指令）、条件编译（conditional compile）、宏（macro）、`mark_debug`（Vivado 综合属性，把信号标记给 ILA 抓波形）。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `boards/create_ip_repo.sh` | **顶层工程路径**的宏生成脚本：把 `BOARD_NAME` 与用户 `DEF` 写进 `ip_config/<ip>_pre_def.v`，再触发 `ip_repo_gen.tcl`。 |
| `ip/create_vivado_proj.sh` | **单 IP 独立路径**的入口：把最多 7 个参数透传给对应 IP 的 `.tcl` 脚本。 |
| `ip/xpu/xpu.tcl`（及 `rx_intf.tcl` / `side_ch.tcl`） | 单 IP 工程脚本：在独立模式下把参数 `append` 进 `./src/<ip>_pre_def.v`。 |
| `boards/ip_repo_gen.tcl` | 顶层打包脚本：循环六个 IP，生成 `clock_speed.v`/`fpga_scale.v`/`has_side_ch_flag.v` 等派生文件，并决定每个 IP 的 `_pre_def.v` 是「覆盖拷贝」还是「追加」。 |
| `boards/openwifi.tcl` | 顶层工程脚本：在 `ip_repo_gen.tcl` 之后**再次覆盖** `clock_speed.v`，是基带时钟的最终决定点。 |
| `ip/board_def.v` | 六个 IP 共享的板卡无关常量契约（采样率等），由它派生出 `NUM_CLK_PER_SAMPLE` 等。 |
| `ip/parse_board_name.tcl` | 由 `BOARD_NAME` 解析出 `fpga_size_flag`（小=0/大=1），驱动 `SMALL_FPGA` 等规模宏。 |
| `ip/tx_intf/src/tx_intf.v`、`ip/side_ch/src/side_ch.v`、`ip/xpu/src/xpu.v` 等 | 宏的**消费端**：用 `` `ifdef `` 裁剪参数/代码块。 |

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：**4.1 `_pre_def.v` 生成机制**（宏从哪里来），**4.2 条件编译宏体系**（宏长什么样、干什么用）。

### 4.1 `_pre_def.v` 生成机制

#### 4.1.1 概念说明

`_pre_def.v` 解决的核心问题是：**如何把构建期的外部信息（板卡名、时钟频率、调试开关）变成 Verilog 在综合时能 `` `ifdef `` 的宏。**

Verilog 没有「命令行 `-D`」这种被各工具链一致支持的入口，openwifi 的做法是：在综合之前，由脚本读命令行参数，把它们翻译成一行行 `` `define <宏名> ``，写进一个 `.v` 文件；需要这些宏的源文件再 `` `include "<ip>_pre_def.v" ``。这样「配置」就变成了「源码的一部分」，但又不用手工维护——每次构建按当时的板卡和参数重新生成。

这个机制有两条互相对齐的路径，必须保持一致：

- **顶层工程路径**（`create_ip_repo.sh`）：一次性给六个 IP 生成 `_pre_def.v`，最终塞进顶层 `openwifi` 工程一起综合。
- **单 IP 独立路径**（`create_vivado_proj.sh` → `<ip>.tcl`）：你单独调试某一个 IP 时，也能用同样的参数注入同样的宏。

README 明确要求：如果你在独立模式用某些 `DEF` 条件编译了一个 IP，那么在顶层用 `create_ip_repo.sh` 重新集成时，**必须传同样的 `DEF`**，否则独立调试与顶层综合的行为不一致。这一点是本讲要重点讲清的。

#### 4.1.2 核心流程

**顶层工程路径（`create_ip_repo.sh`）的宏生成流程**：

```text
1. mkdir -p ip_config; rm -rf ip_config/*        # 清空中间目录
2. BOARD_NAME=${PWD##*/}                         # 由「当前目录名」反推板卡名
3. 对六个 IP 中的每一个:
     用 '>' (覆盖写) 创建 ip_config/<ip>_pre_def.v
     写入第一行: `define <BOARD_NAME>            # 每个 IP 都带板卡宏
4. 遍历命令行剩余参数:
     遇到合法 IP 名 -> 切换"当前 IP", MODULE_NAME=<IP大写>
     否则当作该 IP 的 DEF -> 用 '>>' (追加) 写入:
         `define <MODULE_NAME>_<DEF>
5. source vivado 环境, 跑 ip_repo_gen.tcl (它会把这些文件拷进各 IP 的 src/)
```

**关键约定：宏名 = 大写 IP 名 + 下划线 + 用户 DEF。** 例如用户对 `xpu` 传 `ENABLE_DBG`，生成的宏是 `XPU_ENABLE_DBG`，而不是 `ENABLE_DBG`。这正是「按 IP 加前缀」隔离命名空间的核心。

**单 IP 独立路径（`create_vivado_proj.sh` → `<ip>.tcl`）的宏生成流程**：

```text
1. create_vivado_proj.sh $XILINX_DIR <ip>.tcl [ARG1..ARG7]
     ARG1=BOARD_NAME  ARG2=NUM_CLK_PER_US  ARG3..ARG7=用户 DEF
2. vivado -source <ip>.tcl -tclargs ARG1 ARG2 ... ARG7
3. <ip>.tcl 内:
     MODULE_NAME = <IP大写>   (如 RX_INTF)
     以 'a' (append 追加) 模式打开 ./src/<ip>_pre_def.v
     对 ARG3..ARG7 每个非空值写入: `define <MODULE_NAME>_<ARGx>
```

注意 `xpu.tcl` / `rx_intf.tcl` / `side_ch.tcl` 都是 **append（追加）模式** 打开文件。这意味着在独立模式反复跑同一个脚本时，旧的 `` `define `` 不会被清掉，宏会累积（Verilog 里重复 `` `define `` 同名宏会告警）。所以迭代调试时若改了 `DEF`，最好先删掉 `./src/<ip>_pre_def.v` 再重跑；而顶层 `create_ip_repo.sh` 路径因为有 `rm -rf ip_config/*` + 覆盖写，每次都是干净的。

**顶层打包时 `_pre_def.v` 如何落到 IP 源码目录**（在 `ip_repo_gen.tcl` 的六 IP 循环里）：

```text
对每个 ip_name:
  若 ip_name != openofdm_rx:
      cp ip_config/<ip>_pre_def.v  →  ip/<ip>/src/        # 覆盖式拷贝
  若 ip_name == openofdm_rx:
      打包后用 cat >> 把 ip_config/openofdm_rx_pre_def.v
      「追加」到 ip_repo/openofdm_rx/src/openofdm_rx_pre_def.v   # 追加, 保留子模块自带定义
```

对 `openofdm_rx` 采用**追加而非覆盖**，是因为它是外部 git 子模块（见 u3-l3），自带一份 `openofdm_rx_pre_def.v`（含仿真用的 `SAMPLE_FILE` 等定义）；如果覆盖就会抹掉子模块自己的内容。这正是提交 `b6a3231`（"Change new to append mode for _pre_def.v"）修复的点。

#### 4.1.3 源码精读

**① `create_ip_repo.sh`：先给六个 IP 各写一个板卡宏，再按参数追加用户宏。**

[boards/create_ip_repo.sh:L42-L49](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh#L42-L49) — 遍历六个 IP，用 `>`（覆盖）创建 `ip_config/<ip>_pre_def.v`，并写入两行说明 + `` `define $BOARD_NAME ``。这段代码做的事：为每个 IP 生成一份带板卡标识的预定义文件，并在开头注释里点明「不同 IP 必须用不同文件名」（避免同名冲突）。

```bash
IP_NAME_ALL="xpu tx_intf rx_intf openofdm_tx openofdm_rx side_ch"
for IP_NAME in $IP_NAME_ALL
do
    filename_to_write=ip_config/$IP_NAME"_pre_def.v"
    echo "//Naming pre_def.v differently for all IPs." > $filename_to_write
    echo "//Multiple pre_def.v with different content for different IP are not allowed ..." >> $filename_to_write
    echo "\`define $BOARD_NAME" >> $filename_to_write
done
```

[boards/create_ip_repo.sh:L51-L73](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh#L51-L73) — 这段代码做的事：扫描命令行参数，遇到合法 IP 名就切换「当前 IP」并把 `MODULE_NAME` 设为其**大写形式** `${ARGUMENT^^}`；遇到非 IP 名的参数就当作该 IP 的 `DEF`，追加写入 `` `define ${MODULE_NAME}_${ARGUMENT} ``。这就是 `xpu ENABLE_DBG` → `XPU_ENABLE_DBG` 的转换现场。

```bash
MODULE_NAME=""
for ARGUMENT in "$@"
do
    if [ "$ARGUMENT" = "xpu" ] || [ ... ]; then
        start_to_write=1
    fi
    if [ $start_to_write == "1" ]; then
        if [ 是合法IP名 ]; then
            filename_to_write=ip_config/$ARGUMENT"_pre_def.v"
            MODULE_NAME=${ARGUMENT^^}          # xpu -> XPU
        else
            echo "\`define ${MODULE_NAME}_${ARGUMENT}" >> $filename_to_write
        fi
    fi
done
```

**② `ip_repo_gen.tcl`：决定覆盖还是追加。**

[boards/ip_repo_gen.tcl:L79-L95](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L79-L95) — 这段代码做的事：循环六个 IP；对除 `openofdm_rx` 外的五个自研 IP，把 `ip_config/<ip>_pre_def.v` **覆盖拷贝**到 `ip/<ip>/src/`；唯独对 `openofdm_rx`，在打包完成后用 `cat >>` **追加**到子模块自带的 `openofdm_rx_pre_def.v` 末尾，以保留子模块原有的定义。

```tcl
if {[file exists ./ip_config/$ip_name\_pre_def.v]==0} { ... }   # 兜底建空文件
if {$ip_name != "openofdm_rx"} {
    exec cp ./ip_config/$ip_name\_pre_def.v ../../ip/$ip_name/src/ -f
}
source ../package_ip_complex.tcl
if {$ip_name == "openofdm_rx"} {
    exec cat ./ip_config/$ip_name\_pre_def.v >> ./ip_repo/$ip_name/src/$ip_name\_pre_def.v
}
```

**③ 单 IP 独立路径：`xpu.tcl` 用 append 模式写宏。**

[ip/xpu/xpu.tcl:L50-L78](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/xpu.tcl#L50-L78) — 这段代码做的事：设 `MODULE_NAME=XPU`，以 `'a'`（追加）方式打开 `./src/xpu_pre_def.v`，对 `ARGUMENT3..7` 每个非空值写一行 `` `define XPU_<ARG> ``，最后再写 `` `define $BOARD_NAME ``。`rx_intf.tcl`、`side_ch.tcl` 结构完全相同，只是 `MODULE_NAME` 不同（`RX_INTF` / `SIDE_CH`）。

```tcl
set MODULE_NAME XPU
set  fd  [open  "./src/xpu_pre_def.v"  a]    ;# 注意是 append 模式
if {$ARGUMENT3 eq ""} { puts $fd " " } else { puts $fd "`define $MODULE_NAME\_$ARGUMENT3" }
...
puts $fd "`define $BOARD_NAME"
close $fd
```

[ip/create_vivado_proj.sh:L54-L77](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/create_vivado_proj.sh#L54-L77) — 这段代码做的事：把 `$3..$9` 共 7 个参数装进 `ARG1..ARG7`，再用 `vivado -source $TCL_FILENAME -tclargs ARG1 ... ARG7` 透传给 IP 的 `.tcl`，由后者翻译成 `` `define ``。

**④ Verilog 消费端：用 `` `include `` 拉入预定义，再 `` `ifdef `` 判断。**

[ip/xpu/src/xpu.v:L1-L9](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L1-L9) — 这段代码做的事：先 `` `include "xpu_pre_def.v" `` 拉入构建期生成的宏，再用 `` `ifdef XPU_ENABLE_DBG `` 决定 `DEBUG_PREFIX` 是否展开成 `mark_debug` 综合属性。

```verilog
`include "openwifi_hw_git_rev.v"
`include "xpu_pre_def.v"

`ifdef XPU_ENABLE_DBG
`define DEBUG_PREFIX (*mark_debug="true",DONT_TOUCH="TRUE"*)
`else
`define DEBUG_PREFIX
`endif
```

#### 4.1.4 代码实践

**实践目标**：亲手追踪「用户参数 → 宏名 → 落地文件」的完整链路，确认前缀约定。

**操作步骤**：

1. 读 [boards/create_ip_repo.sh:L65-L68](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh#L65-L68)，找到 `MODULE_NAME=${ARGUMENT^^}` 与 `echo "\`define ${MODULE_NAME}_${ARGUMENT}"`。
2. 假设你在 `boards/zc706_fmcs2/` 目录下执行（不实际运行，仅推理）：

   ```bash
   ../create_ip_repo.sh $XILINX_DIR xpu ENABLE_DBG side_ch ENABLE_DBG
   ```

3. 推理 `ip_config/xpu_pre_def.v` 与 `ip_config/side_ch_pre_def.v` 各会得到哪些 `` `define `` 行。

**需要观察的现象 / 预期结果**（推理结论）：

- `ip_config/xpu_pre_def.v` 包含：
  - `` `define zc706_fmcs2 ``（板卡宏，第 48 行写入）
  - `` `define XPU_ENABLE_DBG ``（用户宏，`xpu`→`XPU` + `ENABLE_DBG`）
- `ip_config/side_ch_pre_def.v` 包含：
  - `` `define zc706_fmcs2 ``
  - `` `define SIDE_CH_ENABLE_DBG ``
- 注意：虽然你两次都传了 `ENABLE_DBG`，但生成的宏名**不同**（`XPU_ENABLE_DBG` vs `SIDE_CH_ENABLE_DBG`），这正是前缀隔离避免命名冲突的作用。
- 若你手头有 Vivado 环境，可在 `boards/zc706_fmcs2/` 实跑一次，再 `cat ip_config/*_pre_def.v` 与 `cat ip/xpu/src/xpu_pre_def.v`、`cat ip/side_ch/src/side_ch_pre_def.v` 验证；否则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `create_ip_repo.sh` 给六个 IP 用六个不同文件名（`xpu_pre_def.v` 等），而不是统一叫 `pre_def.v`？

> **参考答案**：六个 IP 最终会被打包进同一个 Vivado 顶层工程一起综合。同一工程里同名文件的内容必须一致，但每个 IP 需要的 `` `define `` 集合不同（比如只有 `xpu` 需要 `XPU_ENABLE_DBG`）。用不同文件名做隔离，才能让每个 IP 各自携带自己的预定义而不互相覆盖或冲突。`create_ip_repo.sh` 第 46–47 行的注释明确写了这一点。

**练习 2**：独立调试 `rx_intf` 时反复跑 `create_vivado_proj.sh ... rx_intf.tcl ... ENABLE_DBG`，可能出现什么问题？如何规避？

> **参考答案**：`rx_intf.tcl` 以 append（`'a'`）模式打开 `./src/rx_intf_pre_def.v`（见 `rx_intf.tcl:51`），不会清旧内容。反复跑会让 `` `define RX_INTF_ENABLE_DBG `` 重复写入，综合时报「macro redefinition」告警。规避方法：重跑前先删除 `ip/rx_intf/src/rx_intf_pre_def.v`（或确认它本就不存在），让 append 在空文件上起步。

**练习 3**：对 `openofdm_rx` 传 `ENABLE_DBG`，宏名会是什么？它一定会被 `openofdm_rx` 的 Verilog 消费吗？

> **参考答案**：宏名按约定是 `OPENOFDM_RX_ENABLE_DBG`。但 `openofdm_rx` 是外部子模块，是否消费该宏取决于它自己的源码——经检索，自研的 `xpu/tx_intf/rx_intf/side_ch` 确实用 `` `ifdef <IP>_ENABLE_DBG `` 插 `mark_debug`，而 `openofdm_tx`/`openofdm_rx` 并未使用该宏。所以这个 define 会被生成并（以追加方式）写入 `openofdm_rx_pre_def.v`，但若子模块没用它就是无害的空注解。

---

### 4.2 条件编译宏体系

#### 4.2.1 概念说明

如果说 4.1 讲的是「宏的管道」，4.2 讲的就是「管道里流的是什么」。openwifi 的条件编译宏按**来源与用途**可分四类：

1. **板卡派生宏**：由 `BOARD_NAME` 间接决定，自动生成，用户一般不直接传。
   - `` `define <BOARD_NAME> ``（如 `zc706_fmcs2`）——让代码能按板卡分支。
   - `NUM_CLK_PER_US`——每微秒时钟数（= 基带时钟 MHz），驱动所有 1µs 计时。
   - `SMALL_FPGA`——「小容量器件」标志，把 DMA FIFO 深度从 8192 裁到 4096。
   - `SIDE_CH_LESS_BRAM`——专门给 `side_ch` 的 BRAM 裁剪标志。

2. **特性开关宏**：构建脚本里硬编码的「整体功能开关」。
   - `HAS_SIDE_CH` / `NO_SIDE_CH`——是否编译整条侧信道通路。

3. **用户调试宏**：**可选**，由你在命令行用 `DEF` 显式传入。
   - `XPU_ENABLE_DBG`、`TX_INTF_ENABLE_DBG`、`RX_INTF_ENABLE_DBG`、`SIDE_CH_ENABLE_DBG`——打开后给关键信号插 `mark_debug`，配合 ILA 抓片上波形。

4. **运行时配置宏**：构建期生成的「常量」，本质是配置而非开关。
   - `OPENWIFI_HW_GIT_REV`（git 版本号，软件可读）、`SPI_HIGH`/`SPI_LOW`（AD9361 SPI 命令字）。

这四类宏的**共同点**是都不进 git、都在构建期生成；**区别**在于谁来决定它们的值——板卡/脚本/你。

#### 4.2.2 核心流程

下表汇总各宏的「触发源 → 生成文件 → 消费效果」：

| 宏 | 触发源 | 生成处 | 消费效果 |
|----|--------|--------|----------|
| `` `define <BOARD_NAME> `` | 当前目录名 | `ip_config/<ip>_pre_def.v`（`create_ip_repo.sh:48`） | 代码可按板卡 `` `ifdef `` 分支 |
| `NUM_CLK_PER_US` | `openwifi.tcl:21` 顶部 `set` 值 | `ip_repo/clock_speed.v` | 1µs 计数、TSF 心跳、SPI 分频的时钟标尺 |
| `SMALL_FPGA` | `parse_board_name.tcl` 的 `fpga_size_flag==0` | `clock_speed.v`（`ip_repo_gen.tcl:49-51` 与 `openwifi.tcl:24-26`） | `tx_intf/rx_intf/xpu` 的 DMA FIFO：4096 vs 8192 |
| `SIDE_CH_LESS_BRAM` | `fpga_size_flag==0` | `fpga_scale.v`（`ip_repo_gen.tcl:39-41`） | `side_ch` 的 m_axis FIFO 深度裁剪 |
| `HAS_SIDE_CH` / `NO_SIDE_CH` | `ip_repo_gen.tcl:27` 的 `set has_side_ch 1` | `has_side_ch_flag.v`（`ip_repo_gen.tcl:28-34`） | 整个 `side_ch` IP 代码是否编译 |
| `XPU/TX_INTF/RX_INTF/SIDE_CH_ENABLE_DBG` | 命令行 `DEF`（用户传入） | `<ip>_pre_def.v` | `DEBUG_PREFIX` → `mark_debug`，ILA 探针 |
| `OPENWIFI_HW_GIT_REV` | `get_git_rev.sh` | `openwifi_hw_git_rev.v` | 软件可读的 32 位版本号 |
| `SPI_HIGH` / `SPI_LOW` | `ip_repo_gen.tcl:57` 的 `grounded_rf_port` | `spi_command.v` | xpu 控制 AD9361 TX LO 开/关的命令字 |

**派生关系（数学）**：采样率固定 20MHz，基带时钟 `NUM_CLK_PER_US` 决定每样点时钟数：

\[
\text{NUM\_CLK\_PER\_SAMPLE} = \frac{\text{NUM\_CLK\_PER\_US}}{\text{SAMPLING\_RATE\_MHZ}} = \frac{\text{NUM\_CLK\_PER\_US}}{20}
\]

\[
\text{COUNT\_TOP\_1M} = \text{NUM\_CLK\_PER\_US} - 1, \qquad
\text{COUNT\_SCALE} = \frac{\text{NUM\_CLK\_PER\_US}}{10}
\]

例如 100MHz 基带时钟下：每样点 \(\frac{100}{20}=5\) 个时钟；1µs 计数上限 99；与软件 10MHz 计数器的换算标尺 \(\frac{100}{10}=10\)。

**重要陷阱（u2-l4 已点明，这里再强调）**：`NUM_CLK_PER_US` 的真值不在 `board_def.v`（那里只有注释），而在构建期生成的 `clock_speed.v`。`ip_repo_gen.tcl` 先写一版（默认 100），`openwifi.tcl` 又**覆盖**一版（`openwifi.tcl:20-27` 注释写明 "This overrides the value in ip_repo_gen.tcl!"）。所以改基带时钟的唯一正确入口是 `openwifi.tcl:21` 的 `set NUM_CLK_PER_US`。

#### 4.2.3 源码精读

**① 板卡无关契约：`board_def.v` 派生出采样率相关宏。**

[ip/board_def.v:L4-L13](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/board_def.v#L4-L13) — 这段代码做的事：注释说明 `NUM_CLK_PER_US` 来自 `clock_speed.v`（即不在本文件定义），并定义采样率 20MHz、`NUM_CLK_PER_SAMPLE`、`COUNT_TOP_1M`、`COUNT_SCALE` 等派生宏。所有 IP 都 `` `include "board_def.v" `` + `` `include "clock_speed.v" `` 才能拿到完整常量。

```verilog
// clock_speed.v has NUM_CLK_PER_US. The value is determined by .tcl
//`define NUM_CLK_PER_US         100 // 100MHz clock for slow FPGA
`define SAMPLING_RATE_MHZ       20
`define ASSUMED_COUNTER_CLK_MHZ 10
`define NUM_CLK_PER_SAMPLE     ((`NUM_CLK_PER_US)/`SAMPLING_RATE_MHZ)
`define COUNT_TOP_1M           ((`NUM_CLK_PER_US)-1)
`define COUNT_SCALE            ((`NUM_CLK_PER_US)/(`ASSUMED_COUNTER_CLK_MHZ))
```

**② 规模宏的生成：`SMALL_FPGA` 与 `SIDE_CH_LESS_BRAM`。**

[boards/ip_repo_gen.tcl:L37-L53](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L37-L53) — 这段代码做的事：`fpga_size_flag`（来自 `parse_board_name.tcl`）为 0（小器件）时，分别在 `fpga_scale.v` 写 `SIDE_CH_LESS_BRAM`、在 `clock_speed.v` 写 `SMALL_FPGA`，同时写入 `NUM_CLK_PER_US=100`。两个文件分别给 `side_ch` 与 `tx_intf/rx_intf/xpu` 用。

```tcl
# fpga_scale.v (给 side_ch)
if {$fpga_size_flag == 0} { puts $fd "`define SIDE_CH_LESS_BRAM 1" }

# clock_speed.v (给 tx_intf/rx_intf/xpu)
set NUM_CLK_PER_US 100
puts $fd "`define NUM_CLK_PER_US $NUM_CLK_PER_US"
if {$fpga_size_flag == 0} { puts $fd "`define SMALL_FPGA 1" }
```

[ip/parse_board_name.tcl:L7-L80](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/parse_board_name.tcl#L7-L80) — 这段代码做的事：按 `BOARD_NAME` 查表设置 `fpga_size_flag`（小=0，如 `zed_fmcs2`/`zc702_fmcs2`/`antsdr`/`adrv9364z7020`；大=1，如 `zcu102_fmcs2`/`zc706_fmcs2`/`adrv9361z7035`）。这是 `SMALL_FPGA` 等宏的最终来源。

**③ 规模宏的消费：`SMALL_FPGA` 裁剪 DMA FIFO 深度。**

[ip/tx_intf/src/tx_intf.v:L31-L36](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v#L31-L36) — 这段代码做的事：用 `` `ifdef SMALL_FPGA `` 在**参数声明**里二选一——小器件用 4096 深的 DMA FIFO，大器件用 8192。`rx_intf`、`xpu` 中同样的位置也有此裁剪。

```verilog
  parameter integer WAIT_COUNT_BITS = 5,
`ifdef SMALL_FPGA
  parameter integer MAX_NUM_DMA_SYMBOL = 4096
`else
  parameter integer MAX_NUM_DMA_SYMBOL = 8192
`endif
```

[ip/side_ch/src/side_ch.v:L32-L36](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L32-L36) — `side_ch` 用 `SIDE_CH_LESS_BRAM`（而非 `SMALL_FPGA`）裁自己的 m_axis FIFO 深度，作用相同但宏名独立，因为 `side_ch` 不 `` `include "clock_speed.v"``。

**④ 特性开关宏的消费：`HAS_SIDE_CH` 包住整个 IP 体。**

[ip/side_ch/src/side_ch.v:L144](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L144) — `` `ifdef HAS_SIDE_CH `` 出现在模块端口声明之后，把**几乎整个模块体**（function、内部信号、逻辑）包起来。`ip_repo_gen.tcl` 默认 `set has_side_ch 1` 生成 `` `define HAS_SIDE_CH 1 ``，所以默认编译完整侧信道；若改成 0 则生成 `NO_SIDE_CH`，整段代码被裁掉，`side_ch` 退化成空壳（详见 u6-l1）。

**⑤ 用户调试宏的消费：`ENABLE_DBG` → `mark_debug`。**

[ip/rx_intf/src/rx_intf_pl_to_m_axis.v:L8-L14](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v#L8-L14) — 这段代码做的事：拉入 `rx_intf_pre_def.v`，若定义了 `RX_INTF_ENABLE_DBG` 则把 `DEBUG_PREFIX` 展开成 `(*mark_debug="true",DONT_TOUCH="TRUE"*)`，否则为空。随后在端口/信号声明前缀 `` `DEBUG_PREFIX ``，就只在调试构建里把这些信号挂上 ILA。

```verilog
`include "rx_intf_pre_def.v"

`ifdef RX_INTF_ENABLE_DBG
`define DEBUG_PREFIX (*mark_debug="true",DONT_TOUCH="TRUE"*)
`else
`define DEBUG_PREFIX
`endif
```

> 经检索，`XPU_ENABLE_DBG`、`TX_INTF_ENABLE_DBG`、`RX_INTF_ENABLE_DBG`、`SIDE_CH_ENABLE_DBG` 这四个自研 IP 宏被真实消费（插入 `mark_debug`/`DEBUG_PREFIX`）；而 `openofdm_tx` / `openofdm_rx`（基于 openofdm 项目）并未消费同名宏——传了也无害，但不会产生 ILA 探针。

**⑥ 顶层时钟覆盖：基带时钟的最终决定点。**

[boards/openwifi.tcl:L20-L30](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L20-L30) — 这段代码做的事：在 `ip_repo_gen.tcl` 之后**重新覆盖** `clock_speed.v`，写入 `NUM_CLK_PER_US=100`（可在此改成 200/240），并按 `fpga_size_flag` 决定是否写 `SMALL_FPGA`，再拷给 `tx_intf/rx_intf/xpu`。这是 README 说的「改基带时钟入口」。

```tcl
# This overrides the value in ip_repo_gen.tcl!
set NUM_CLK_PER_US 100
set  fd  [open  "./ip_repo/clock_speed.v"  w]
puts $fd "`define NUM_CLK_PER_US $NUM_CLK_PER_US"
if {$fpga_size_flag == 0} { puts $fd "`define SMALL_FPGA 1" }
close $fd
```

#### 4.2.4 代码实践

**实践目标**：依据 README 与脚本，写出为所有 IP 启用 ILA/DEBUG 宏的命令，并说明每个 `` `define `` 落进哪个文件。

**操作步骤**：

1. 读 [README.md:L131-L156](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L131-L156) 的 "Conditional compile by verilog macro" 小节，找到官方示例命令。
2. 进入某板卡目录（例如 `cd openwifi-hw/boards/zc706_fmcs2/`），执行：

   ```bash
   ../create_ip_repo.sh $XILINX_DIR \
       xpu ENABLE_DBG tx_intf ENABLE_DBG rx_intf ENABLE_DBG \
       openofdm_tx ENABLE_DBG openofdm_rx ENABLE_DBG side_ch ENABLE_DBG
   ```

3. 不实际综合，仅推理每个 `` `define `` 的名字与落地文件。

**需要观察的现象 / 预期结果**：

- 该命令对每个 IP 都传了 `ENABLE_DBG`，按「大写 IP 名 + `_ENABLE_DBG`」约定，六个 IP 的 `ip_config/<ip>_pre_def.v` 分别多出一行：

  | IP | 生成的宏 | 中间文件 | 最终文件 |
  |----|---------|---------|---------|
  | xpu | `` `define XPU_ENABLE_DBG `` | `ip_config/xpu_pre_def.v` | `ip/xpu/src/xpu_pre_def.v` |
  | tx_intf | `` `define TX_INTF_ENABLE_DBG `` | `ip_config/tx_intf_pre_def.v` | `ip/tx_intf/src/tx_intf_pre_def.v` |
  | rx_intf | `` `define RX_INTF_ENABLE_DBG `` | `ip_config/rx_intf_pre_def.v` | `ip/rx_intf/src/rx_intf_pre_def.v` |
  | openofdm_tx | `` `define OPENOFDM_TX_ENABLE_DBG `` | `ip_config/openofdm_tx_pre_def.v` | `ip/openofdm_tx/src/openofdm_tx_pre_def.v` |
  | openofdm_rx | `` `define OPENOFDM_RX_ENABLE_DBG `` | `ip_config/openofdm_rx_pre_def.v` | 追加到 `ip/openofdm_rx/.../openofdm_rx_pre_def.v` |
  | side_ch | `` `define SIDE_CH_ENABLE_DBG `` | `ip_config/side_ch_pre_def.v` | `ip/side_ch/src/side_ch_pre_def.v` |

- 其中自研四个 IP（xpu/tx_intf/rx_intf/side_ch）的宏会真正触发 `DEBUG_PREFIX` → `mark_debug`，综合后可用 ILA 抓波形；openofdm_tx/openofdm_rx 的宏被生成但不被消费（无害）。
- 实际效果需在 Vivado 中 `Generate Bitstream` 后用 `.ltx` 打开 ILA 验证——若手头无环境，标注「待本地验证」。

> **提示**：这正对应 README L153-L156 的官方示例；答案就是这条命令本身，而「写到哪个文件」即上表的「中间文件 → 最终文件」两列。

#### 4.2.5 小练习与答案

**练习 1**：把 `zcu102_fmcs2`（大器件，`fpga_size_flag=1`）和 `zed_fmcs2`（小器件，`fpga_size_flag=0`）相比，`tx_intf` 的 `MAX_NUM_DMA_SYMBOL` 分别是多少？为什么？

> **参考答案**：`zcu102_fmcs2` 为 8192，`zed_fmcs2` 为 4096。因为 `parse_board_name.tcl` 给前者设 `fpga_size_flag=1`、后者设 0；`ip_repo_gen.tcl:49-51`/`openwifi.tcl:24-26` 只在 `fpga_size_flag==0` 时写 `` `define SMALL_FPGA 1 ``；`tx_intf.v:31-36` 在 `` `ifdef SMALL_FPGA `` 下选 4096，否则 8192。小器件 BRAM 资源少，故裁半。

**练习 2**：如果你想让基带时钟从 100MHz 改成 200MHz（假设板卡支持），应该改哪里？改完后 `NUM_CLK_PER_SAMPLE` 变成多少？

> **参考答案**：改 `boards/openwifi.tcl:21` 的 `set NUM_CLK_PER_US 100` 为 `200`（不是改 `board_def.v`，那里只有注释）。改完重新 `source openwifi.tcl`。`NUM_CLK_PER_SAMPLE = 200/20 = 10`（原来 100/20=5），即每样点从 5 个时钟变 10 个时钟。注意并非所有板卡都支持 200MHz（README 指出仅 zc706 与 adrv9361z7035 支持 100/200，zcu102 支持 240/100，其余仅 100）。

**练习 3**：`HAS_SIDE_CH` 与 `SMALL_FPGA` 都是「构建期自动决定」的宏，但触发逻辑不同。请说明二者分别由哪个脚本的哪个变量驱动。

> **参考答案**：`SMALL_FPGA` 由 `parse_board_name.tcl` 按板卡查表得到的 `fpga_size_flag` 驱动（`fpga_size_flag==0` 时写），完全由板卡决定；`HAS_SIDE_CH` 由 `ip_repo_gen.tcl:27` 的 `set has_side_ch 1` 驱动，是脚本里硬编码的开关（想关 side_ch 就改成 0），与板卡无关。

## 5. 综合实践

**任务**：给一块指定板卡（以 `zc706_fmcs2` 为例）设计一份「条件编译配置说明」，把本讲所有知识点串起来。

要求产出一份文档，包含：

1. **板卡画像**：查 `parse_board_name.tcl`，列出 `zc706_fmcs2` 的 `fpga_size_flag`、`ultra_scale_flag`、`part_string`，并据此推断它会自动得到哪些规模宏（`SMALL_FPGA`？`SIDE_CH_LESS_BRAM`？）。
2. **时钟配置**：写出默认 `NUM_CLK_PER_US`，并计算 `NUM_CLK_PER_SAMPLE`、`COUNT_TOP_1M`、`COUNT_SCALE` 三个派生值（用 `board_def.v` 的公式）。
3. **调试开关命令**：写出为该板卡所有自研 IP 启用调试的 `create_ip_repo.sh` 命令，列出每个生成的宏名与落地文件。
4. **差异分析**：对比若改用 `zed_fmcs2`（小器件），上述 1、2 中哪些值会不同，`tx_intf` 的 FIFO 深度会怎么变。
5. **验证方式**：说明如何在 Vivado 里确认这些宏真的生效（提示：综合后看 `MAX_NUM_DMA_SYMBOL` 实际值、看 ILA 是否插入了探针）。

**参考思路**：

- 第 1 步：`zc706_fmcs2` → `fpga_size_flag=1`（大）、`ultra_scale_flag=0`、`part_string=xc7z045ffg900-2`；因 `fpga_size_flag=1`，**不会**得到 `SMALL_FPGA`/`SIDE_CH_LESS_BRAM`。
- 第 2 步：默认 `NUM_CLK_PER_US=100`；`NUM_CLK_PER_SAMPLE=5`、`COUNT_TOP_1M=99`、`COUNT_SCALE=10`。
- 第 3 步：见 4.2.4 的命令与表格（注意 `zc706_fmcs2` 支持 200MHz，可额外讨论改成 200 的影响）。
- 第 4 步：`zed_fmcs2` 的 `fpga_size_flag=0`，会得到 `SMALL_FPGA` 与 `SIDE_CH_LESS_BRAM`，`tx_intf` 的 `MAX_NUM_DMA_SYMBOL` 从 8192 降到 4096；`NUM_CLK_PER_US` 仍为 100（zed 仅支持 100MHz）。
- 第 5 步：综合后打开 `tx_intf` 的 utilization 报告或 elaborate 后看参数；ILA 探针需在 `Generate Bitstream` 后用配套 `.ltx` 打开 Hardware Manager 验证。手头无板卡时相关现象标注「待本地验证」。

## 6. 本讲小结

- `_pre_def.v` 是构建期现场生成的「配置快照」，把命令行参数翻译成 Verilog `` `define ``，本身不进 git。
- 宏名遵循 **`<大写IP名>_<用户DEF>`** 约定（如 `XPU_ENABLE_DBG`），配合按 IP 区分的文件名，解决六 IP 同工程下的命名冲突。
- 有两条对齐的注入路径：顶层 `create_ip_repo.sh`（覆盖式，每次干净）与单 IP `create_vivado_proj.sh` → `<ip>.tcl`（append 式，迭代时注意清旧）。
- 宏分四类：板卡派生（`BOARD_NAME`/`NUM_CLK_PER_US`/`SMALL_FPGA`/`SIDE_CH_LESS_BRAM`）、特性开关（`HAS_SIDE_CH`/`NO_SIDE_CH`）、用户调试（`*_ENABLE_DBG` → `mark_debug`）、运行时配置（`OPENWIFI_HW_GIT_REV`/`SPI_*`）。
- `NUM_CLK_PER_US` 的真值在 `clock_speed.v`，且被 `openwifi.tcl` 二次覆盖——改基带时钟的唯一正确入口是 `openwifi.tcl:21`。
- 为所有 IP 开 ILA 的命令就是 README L155 那条；其中四个自研 IP 真正消费 `*_ENABLE_DBG`，`openofdm_*` 不消费（无害）。

## 7. 下一步学习建议

- 想亲手在 Vivado 里跑单 IP 仿真、看 `_pre_def.v` 如何影响行为，继续学 **u7-l3（IP 仿真与 testbench 实践）**，那里会用到 `create_vivado_proj.sh` 与 `openofdm_rx_pre_def.v` 里的 `SAMPLE_FILE` 仿真定义。
- 想把带调试宏的 IP 重新集成进顶层工程，学 **u7-l4（修改并打包自定义 IP）**，把 `create_ip_repo.sh` → `ip_repo_gen.tcl` → `package_ip_complex.tcl` 的打包链路连起来。
- 想了解 `mark_debug` 之外的硬件级调试手段（LED/GPIO 映射、`.ltx` 抓波形），学 **u7-l6（GPIO/LED 调试、ILA 与 ENABLE_DBG）**，它是本讲「用户调试宏」一节的实战延续。
- 若关心这些宏背后寄存器与软件如何交互，回顾 **u7-l1（AXI 寄存器映射与软件交互）**；关心时钟宏如何驱动 MAC 定时，回顾 **u2-l4（板级配置与时钟体系）** 与 **u5-l2（CSMA/CA）**。
