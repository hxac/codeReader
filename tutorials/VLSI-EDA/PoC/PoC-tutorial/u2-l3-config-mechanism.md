# 配置机制：my_config 与 config 包

## 1. 本讲目标

PoC 是一个「同一份源码要在 Altera、Xilinx、Lattice、Microsemi 多家厂商器件上跑」的 IP 核库。要做到这点，它必须先知道「我现在要综合到哪块板、哪颗芯片」。本讲就讲解这套**配置机制**：用户在两个模板文件里写两个字符串常量，公共包 `config` 把它们解析成结构化的器件信息（`DEVICE_INFO`、`VENDOR`），后续每一个核再据此自动选择厂商专用实现。

学完本讲你应该能够：

- 说清 `my_config.vhdl` / `my_project.vhdl` 两个模板里各常量的含义，以及它们如何进入编译流程。
- 复述 `config.vhdl` 把器件字符串（如 `XC7K325T-2FFG900C`）一步步拆解成厂商、器件、系列、收发器类型的过程。
- 列出 `T_DEVICE_INFO` 记录包含的字段，并解释为什么 `sync_Bits` 这样的核能凭 `DEVICE_INFO.Vendor` 自动切换实现。

## 2. 前置知识

本讲假定你已经学过 u1-l3（知道要从模板复制出本地 `my_config.vhdl` / `my_project.vhdl`）和 u2-l1（知道 `src/common/` 下的公共包、`common.files` 编译清单、`context PoC.common`）。下面把几个本讲会用到的概念快速澄清。

- **IP 核与厂商专用原语**：同样是「两级 D 触发器同步器」，Xilinx 推荐用专用原语并加 `ASYNC_REG` 约束，Altera 有自己的优化版本。PoC 用一个「通用包装实体」套多个「厂商专用子实体」，靠配置信息决定真正综合哪一份。这是本讲的「为什么」。
- **VHDL 的字符串与定长填充**：VHDL 的 `string` 长度属于类型的一部分，不同长度的字符串不能直接比较或塞进同一个数组。PoC 用一个特殊填充字符 `C_POC_NUL`（`'~'`）把所有变长字符串补齐到固定长度（如 64），从而能存进 `C_BOARD_INFO_LIST` 这样的查找表。理解这点，才能读懂后面的 `conf()` 函数。
- **配置即编译期常量**：本讲涉及的所有「解析」都发生在** elaboration（确立）阶段**——也就是综合/仿真前 VHDL 工具计算常量与 `if generate` 条件的时候。换言之，`VENDOR` 一旦在 `my_config` 里定下，整条调用链上的厂商分支在综合时就已固化，运行时不再改变。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/common/my_config.vhdl.template](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/my_config.vhdl.template) | 描述**目标硬件**的模板：板名 `MY_BOARD`、器件型号 `MY_DEVICE`。复制去掉 `.template` 后缀后由用户填写。 |
| [src/common/my_project.vhdl.template](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/my_project.vhdl.template) | 描述**宿主环境**的模板：工程目录 `MY_PROJECT_DIR`、操作系统 `MY_OPERATING_SYSTEM`。 |
| [src/common/config.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl) | 核心解析包。内含两个 package：`config_private`（板级描述表与定长字符串工具）和 `config`（厂商/器件枚举、解析函数、`T_DEVICE_INFO` 记录）。 |
| [src/common/common.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files) | 编译清单，规定 `my_config` / `my_project` 必须在 `config.vhdl` **之前**进入 PoC 库。 |
| [src/misc/sync/sync_Bits.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl) | 一个「消费方」示例：用 `DEVICE_INFO.Vendor` 在通用/Altera/Xilinx 三套同步器实现间做 `generate` 选择。 |
| [tb/common/my_config.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config.files) | pyIPCMI 用的条件清单：按 `BoardName` 选择对应预置 `my_config_<board>.vhdl`。 |
| [tb/common/config_tb.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/config_tb.vhdl) | `config` 包的测试台，演示如何把解析结果打印出来并断言。 |

> ⚠️ **现实观察**：`my_config.vhdl.template` 的文档头注释里写着「the global packages common/config **and common/board** evaluate the settings」，但仓库里**并不存在** `board.vhdl`——板的描述（`C_BOARD_INFO_LIST`）实际上已被合并进 `config.vhdl` 的 `config_private` 包。注释是历史遗留，以源码为准。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应配置数据的「输入 → 解析 → 输出」三段：

1. **my_config / my_project 模板**：用户填写的入口。
2. **config 解析**：模板里的字符串如何被一步步拆解。
3. **DEVICE_INFO 与 VENDOR 枚举**：解析结果如何驱动厂商自动选择。

---

### 4.1 my_config / my_project 模板：本地配置的入口

#### 4.1.1 概念说明

PoC 仓库里只提供**模板**（文件名带 `.template` 后缀）。每个使用者要做的，是把模板复制成不带后缀的真文件，并填上自己的值。这一步 u1-l3 已经演示过，本讲聚焦在「这两个文件里到底声明了什么、被谁读取」。

PoC 把「配置」分成两类，放在两个独立文件里，原因很直觉——它们描述的是两件不同的事：

| 文件 | 描述对象 | 何时用到 |
| --- | --- | --- |
| `my_config.vhdl` | **目标硬件**（要烧到哪块板、哪颗 FPGA） | 影响综合结果（选哪个厂商实现） |
| `my_project.vhdl` | **宿主环境**（你的电脑工程目录、操作系统） | 影响文件路径、工具链调用（多在 pyIPCMI 脚本侧） |

#### 4.1.2 核心流程

```text
模板（仓库提供）                        本地真文件（用户复制+填写）
─────────────────                      ─────────────────────────
my_config.vhdl.template   ──复制──>    my_config.vhdl      填 MY_BOARD / MY_DEVICE / MY_VERBOSE
my_project.vhdl.template  ──复制──>    my_project.vhdl     填 MY_PROJECT_DIR / MY_OPERATING_SYSTEM
                                                │
                                                │  被加入 PoC 库（common.files 规定先于 config.vhdl 编译）
                                                ▼
                                  config 包 use PoC.my_config.all / PoC.my_project.all 读取
```

注意一个关键细节：`my_config` / `my_project` 是**两个独立的 VHDL 包**，里面只是裸的 `constant` 声明，没有任何逻辑。真正的解析逻辑全部在 `config` 包里。这样设计的好处是：用户文件极简、不易出错；解析逻辑集中、可维护。

#### 4.1.3 源码精读

`my_config.vhdl.template` 只声明三个常量，见 [src/common/my_config.vhdl.template:46-53](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/my_config.vhdl.template#L46-L53)：

```vhdl
package my_config is
  constant MY_BOARD   : string  := "CHANGE THIS";  -- e.g. Custom, ML505, KC705, Atlys
  constant MY_DEVICE  : string  := "CHANGE THIS";  -- e.g. None, XC5VLX50T-1FF1136, EP2SGX90FF1508C3
  constant MY_VERBOSE : boolean := FALSE;          -- activate detailed report statements
end package;
```

- `MY_BOARD` 是**板名**，值必须是 `config_private.C_BOARD_INFO_LIST` 里已知的一个名字（如 `KC705`、`Atlys`、`ML505`），或保留名 `Custom` / `GENERIC`。
- `MY_DEVICE` 是**器件型号字符串**。可填完整型号（如 `XC7K325T-2FFG900C`），也可填 `"None"` 表示「我不指定器件，让它从板名推断」。
- `MY_VERBOSE` 是内部开关，置 `TRUE` 时解析函数会打印详细 `report`，便于排错。

`my_project.vhdl.template` 更短，见 [src/common/my_project.vhdl.template:43-47](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/my_project.vhdl.template#L43-L47)：

```vhdl
package my_project is
  constant MY_PROJECT_DIR        : string := "CHANGE THIS";  -- e.g. "/home/me/projects/myproject/"
  constant MY_OPERATING_SYSTEM   : string := "CHANGE THIS";  -- e.g. "WINDOWS", "LINUX"
end package;
```

这两个常量主要被 pyIPCMI（Python 侧）和少量需要拼路径的包消费，本讲不展开。

那么 `config` 包**怎么读到**这些常量？靠 `use` 子句，见 [src/common/config.vhdl:376-380](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L376-L380)：

```vhdl
library PoC;
use     PoC.my_config.all;        -- 读到 MY_BOARD / MY_DEVICE / MY_VERBOSE
use     PoC.my_project.all;       -- 读到 MY_PROJECT_DIR / MY_OPERATING_SYSTEM
use     PoC.config_private.all;
use     PoC.utils.all;
```

这四行决定了**编译顺序**：`my_config` 与 `my_project` 必须先于 `config` 进入 `PoC` 库。这个顺序由 `common.files` 保证，见 [src/common/common.files:8-12](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files#L8-L12)：

```text
include  "tb/common/my_config.files"   # board and device configuration  ← 先编译 my_config/my_project
vhdl poc "src/common/utils.vhdl"
vhdl poc "src/common/config.vhdl"      # ← 之后才编译 config
```

`config` 包一开头就把 `my_config`/`my_project` 的裸常量「重新导出」成更稳定的名字，见 [src/common/config.vhdl:384-386](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L384-L386)：

```vhdl
constant PROJECT_DIR       : string  := MY_PROJECT_DIR;
constant OPERATING_SYSTEM  : string  := MY_OPERATING_SYSTEM;
constant POC_VERBOSE       : boolean := MY_VERBOSE;
```

这样下游只需 `use PoC.config.all`，就能拿到统一的 `POC_VERBOSE` 等名字，而不必直接依赖 `my_config`。

仿真场景下，pyIPCMI 用 [tb/common/my_config.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config.files) 按 `BoardName` 选择一个**预置**的 `my_config_<board>.vhdl`（例如 `BoardName = "KC705"` 时选 `my_config_KC705.vhdl`）。以 KC705 为例，见 [tb/common/my_config_KC705.vhdl:36-43](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config_KC705.vhdl#L36-L43)：

```vhdl
constant MY_BOARD  : string := "KC705";
constant MY_DEVICE : string := "None";   -- infer from MY_BOARD
```

也就是说，仿真时板名定下，器件由 `MY_BOARD` 推断——这正是下一节要讲的「回退」路径。

#### 4.1.4 代码实践

**实践目标**：亲手把模板变成可用配置，并验证它会被 `config` 读到。

**操作步骤**：

1. 复制模板（如果你还没做 u1-l3 的练习）：
   ```bash
   cp src/common/my_config.vhdl.template  src/common/my_config.vhdl
   cp src/common/my_project.vhdl.template src/common/my_project.vhdl
   ```
2. 打开新生成的 `src/common/my_config.vhdl`，把两行改成：
   ```vhdl
   constant MY_BOARD  : string  := "Custom";
   constant MY_DEVICE : string  := "XC7K325T-2FFG900C";
   constant MY_VERBOSE: boolean := TRUE;   -- 打开详细日志，便于观察解析
   ```
3. 在 `config.vhdl` 中确认第 377 行 `use PoC.my_config.all;` 确实引用了 `MY_DEVICE`。

**需要观察的现象**：把 `MY_VERBOSE` 设为 `TRUE` 后，综合或仿真确立阶段，`getLocalDeviceString` 等函数会通过 `report ... severity NOTE` 打印出解析到的器件字符串。

**预期结果**：日志里能看到类似 `getLocalDeviceString: ... MY_DEVICE='XC7K325T-2FFG900C' ...` 的行。若你看到的是 `Unknown vendor` 之类的 `severity failure`，说明字符串前缀没被识别——回到 4.2 节核对前缀。

> 若本地暂无 VHDL 工具链，可标注「待本地验证」：仅阅读源码也能确认 `use PoC.my_config.all` 把 `MY_DEVICE` 引入 `config` 的作用域。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MY_DEVICE` 可以填 `"None"`？填 `"None"` 后系统从哪里得到器件信息？

**参考答案**：`"None"` 是一个显式的「我不指定器件」标记。`config` 包里的 `getLocalDeviceString` 在检测到 `MY_DEVICE` 为 `"None"` 时，会回退到 `MY_BOARD`，再用板名查 `C_BOARD_INFO_LIST` 表得到该板对应的 `FPGADevice`（例如 `KC705` → `XC7K325T-2FFG900C`）。详见 4.2 节。

**练习 2**：`my_config` 与 `my_project` 都是包，但里面只有 `constant`、没有任何函数体。这种设计有什么好处？

**参考答案**：把「易变、因人而异」的配置值与「稳定、全库共用」的解析逻辑彻底分离。用户只需改两个字符串常量，几乎不会写错；而解析逻辑集中在 `config` 包，升级或修 bug 只需改一处。

---

### 4.2 config 解析：从字符串到结构化器件信息

#### 4.2.1 概念说明

`MY_DEVICE` 只是一个**字符串**（如 `XC7K325T-2FFG900C` 或 `EP4SGX230KF40C2`），但后续核需要的是**枚举值**（`VENDOR_XILINX`、`DEVICE_KINTEX7`）和**数值**（器件号 `325`）。`config` 包的核心职责，就是用一组纯函数把字符串**解析**成这些结构化结果。

这里有个伏笔要先点破：FPGA 器件型号字符串其实是有规律的——前缀编码厂商，第 3~4 位编码器件系列。PoC 正是利用了这套「厂商型号命名约定」来做模式匹配。例如：

- `XC…` → Xilinx（`XC` 是 Xilinx 的经典前缀）
- `EP…` → Altera/Intel（`EP` 是 Altera 的器件前缀）
- `LFE…` → Lattice ECP 系列
- `MPF…` → Microsemi PolarFire

所以「解析」本质上就是**按位置切片 + 模式匹配**。

#### 4.2.2 核心流程

完整的解析链路如下：

```text
                 MY_DEVICE (string)            MY_BOARD (string)
                       │                              │
                       │   getLocalDeviceString       │  BOARD() → C_BOARD_INFO_LIST[i].FPGADevice
                       ▼                              │
            优先级：显式入参 > MY_DEVICE > MY_BOARD 的器件 ◄──┘
                       │
                       ▼
              归一化为定长 32 字符串 MY_DEV
                       │
          ┌────────────┼─────────────┬──────────────┐
          ▼            ▼             ▼              ▼
       VENDOR()     DEVICE()    DEVICE_FAMILY()   …（DEVICE_SERIES / DEVICE_NUMBER / DEVICE_SUBTYPE …）
   看 MY_DEV(1-2/1-3)  看 MY_DEV(3-4)   看 MY_DEV(4)
          │            │             │
          └────────────┴─────────────┴──► 汇总进 DEVICE_INFO()  ──► T_DEVICE_INFO 记录
```

解析有两条入口：

1. **器件入口**（`MY_DEVICE`）：经 `getLocalDeviceString` 归一化后，分别喂给 `VENDOR` / `DEVICE` / `DEVICE_FAMILY` 等函数。
2. **板入口**（`MY_BOARD`）：经 `BOARD()` 在 `C_BOARD_INFO_LIST` 查表，得到该板的 `FPGADevice` 字符串，再走器件入口。

#### 4.2.3 源码精读

**(a) 定长字符串与 `conf()`**

为了让变长字符串能进查找表，`config_private` 定义了一组定长子类型，见 [src/common/config.vhdl:44-46](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L44-L46)：

```vhdl
subtype T_BOARD_STRING         is string(1 to 16);
subtype T_BOARD_CONFIG_STRING  is string(1 to 64);
subtype T_DEVICE_STRING        is string(1 to 32);
```

并用 `'~'` 作为填充字符（命名为 `C_POC_NUL`，类比 C 的 `'\0'`），见 [src/common/config.vhdl:91-94](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L91-L94)：

```vhdl
constant C_POC_NUL                  : character := '~';
constant C_BOARD_CONFIG_STRING_EMPTY: T_BOARD_CONFIG_STRING := (others => C_POC_NUL);
constant C_DEVICE_STRING_EMPTY      : T_DEVICE_STRING       := (others => C_POC_NUL);
```

`conf()` 把任意字符串补齐到 64 字符，见 [src/common/config.vhdl:96-105](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L96-L105)：

```vhdl
function conf(str : string) return T_BOARD_CONFIG_STRING is
  variable Result : string(1 to T_BOARD_CONFIG_STRING'length);
begin
  Result := (others => C_POC_NUL);                                  -- 先全填 '~'
  if (str'length > 0) then
    Result(1 to bound(...)) := ite(..., str(1 to imin(64, str'length)), ConstNUL);
  end if;
  return Result;
end function;
```

效果：`conf("KC705")` 得到 `"KC705~~~~~~~~~~…"`（共 64 字符）。后续比较时用 `str_imatch` 忽略尾部 `~` 即可。

**(b) 板描述表 `C_BOARD_INFO_LIST`**

板的信息集中存成一张表，见 [src/common/config.vhdl:158-368](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L158-L368)。每条记录的结构由 `T_BOARD_INFO` 定义，见 [src/common/config.vhdl:70-76](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L70-L76)：

```vhdl
type T_BOARD_INFO is record
  BoardName     : T_BOARD_CONFIG_STRING;
  FPGADevice    : T_BOARD_CONFIG_STRING;   -- 关键字段：板对应的器件型号
  UART          : T_BOARD_UART_DESC;
  Ethernet      : T_BOARD_ETHERNET_DESC_VECTOR(...);
  EthernetCount : T_BOARD_ETHERNET_DESC_INDEX;
end record;
```

例如 KC705 那条，见 [src/common/config.vhdl:296-303](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L296-L303)：

```vhdl
( BoardName => conf("KC705"),
  FPGADevice => conf("XC7K325T-2FFG900C"),   -- 板上的 FPGA 型号
  ... )
```

> 注意表尾的 `Custom` 条目（[src/common/config.vhdl:361-367](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L361-L367)）注释写着 `MUST BE LAST ONE`——查表时按顺序匹配，`Custom` 是兜底。

**(c) 三级优先的 `getLocalDeviceString`**

这是「器件字符串到底从哪来」的核心。见 [src/common/config.vhdl:657-678](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L657-L678)，逻辑可概括为：

```vhdl
function getLocalDeviceString(DeviceString : string) return string is
  constant MY_DEVICE_STR : string := BOARD_DEVICE;   -- 由 MY_BOARD 查表得到的器件
begin
  Result := (others => C_POC_NUL);
  if (str_length(DeviceString) /= 0) and not str_imatch(DeviceString, "None") then
    -- ① 显式入参优先（函数被外部调用并传了具体型号）
  elsif (str_length(MY_DEVICE) /= 0) and not str_imatch(MY_DEVICE, "None") then
    -- ② 否则用 my_config 里的 MY_DEVICE
  else
    -- ③ 最后回退到 MY_BOARD 推断出的器件 MY_DEVICE_STR
  end if;
end function;
```

默认调用时不传 `DeviceString`，所以走的是 ②→③。当 `MY_DEVICE="None"` 时落到 ③，即由 `MY_BOARD` 经 `BOARD_DEVICE` 推断。

**(d) 厂商识别 `VENDOR`**

这是「按前缀切片匹配」的典范。见 [src/common/config.vhdl:762-781](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L762-L781)：

```vhdl
function VENDOR(DeviceString : string := C_DEVICE_STRING_EMPTY) return T_VENDOR is
  constant MY_DEV   : string(1 to 32) := getLocalDeviceString(DeviceString);
  constant VEN_STR2 : string(1 to 2)   := MY_DEV(1 to 2);
  constant VEN_STR3 : string(1 to 3)   := MY_DEV(1 to 3);
begin
  case VEN_STR2 is
    when "GE" => return VENDOR_GENERIC;
    when "EP" => return VENDOR_ALTERA;
    when "XC" => return VENDOR_XILINX;     -- Xilinx 的 XC 前缀
    when others => null;
  end case;
  case VEN_STR3 is
    when "MPF" => return VENDOR_MICROSEMI;  -- PolarFire
    when "iCE" => return VENDOR_LATTICE;    -- iCE 系列
    when "LCM" => return VENDOR_LATTICE;    -- MachXO
    when "LFE" => return VENDOR_LATTICE;    -- ECP 系列
    when others => report "Unknown vendor ..." severity failure;
  end case;
end function;
```

逻辑很直白：先看前 2 个字符，能定就定（`XC`→Xilinx、`EP`→Altera、`GE`→Generic）；定不了再看前 3 个字符（`LFE`→Lattice ECP 等）。都匹配不上则 `severity failure` 直接报错中止。

**(e) 器件识别 `DEVICE` 与系列 `DEVICE_FAMILY`**

`VENDOR` 定下后，`DEVICE` 再用**第 3~4 位**判断具体型号。Xilinx 分支见 [src/common/config.vhdl:845-859](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L845-L859)：

```vhdl
when VENDOR_XILINX =>
  case DEV_STR is       -- DEV_STR = MY_DEV(3 to 4)
    when "7A" => return DEVICE_ARTIX7;
    when "7K" => return DEVICE_KINTEX7;     -- XC7K325T 的 "7K" → Kintex-7
    when "7Z" => return DEVICE_ZYNQ7;
    when "5V" => return DEVICE_VIRTEX5;
    ...
  end case;
```

`DEVICE_FAMILY` 则只看**第 4 个字符**，见 [src/common/config.vhdl:889-897](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L889-L897)：

```vhdl
when VENDOR_XILINX =>
  case FAM_CHAR is      -- FAM_CHAR = MY_DEV(4)
    when 'A' => return DEVICE_FAMILY_ARTIX;
    when 'K' => return DEVICE_FAMILY_KINTEX;  -- XC7K... 第 4 位 'K' → Kintex 家族
    when 'S' => return DEVICE_FAMILY_SPARTAN;
    when 'V' => return DEVICE_FAMILY_VIRTEX;
    when 'Z' => return DEVICE_FAMILY_ZYNQ;
  end case;
```

以 `XC7K325T-2FFG900C` 为例，解析轨迹是：

| 步骤 | 切片 | 结果 |
| --- | --- | --- |
| `VENDOR` | `MY_DEV(1 to 2)` = `"XC"` | `VENDOR_XILINX` |
| `DEVICE` | `MY_DEV(3 to 4)` = `"7K"` | `DEVICE_KINTEX7` |
| `DEVICE_FAMILY` | `MY_DEV(4)` = `'K'` | `DEVICE_FAMILY_KINTEX` |
| `DEVICE_NUMBER` | `MY_DEV(5 to end)` = `"325T-2FFG900C"` → `extractFirstNumber` | `325` |
| `DEVICE_SUBTYPE` | 查 `MY_DEV(5 to 6)` + 是否含 `"T"` | `DEVICE_SUBTYPE_T`（Kintex-7 命中 `'T'`） |
| `DEVICE_SERIES` | 由 `DEVICE_KINTEX7` 推 | `DEVICE_SERIES_7_SERIES` |

> 小细节：`DEVICE_NUMBER` 用 `extractFirstNumber`（[src/common/config.vhdl:680-715](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L680-L715)）手写解析数字，而不是用 VHDL 的 `integer'value(...)`。注释 [src/common/config.vhdl:705](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L705) 说明原因：`'value` 在 Vivado Synth 2014.1 中不被支持。这是典型的「为兼容旧工具链而绕路」。

#### 4.2.4 代码实践

**实践目标**：把一个 Xilinx 型号喂进解析链，验证它能被识别为 `VENDOR_XILINX`。

**操作步骤**：

1. 按 4.1.4 把 `MY_DEVICE` 设为 `"XC7K325T-2FFG900C"`，`MY_VERBOSE` 设为 `TRUE`。
2. 打开 [tb/common/config_tb.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/config_tb.vhdl)。它的进程里有一串 `report` 把解析结果打印出来，见 [tb/common/config_tb.vhdl:53-63](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/config_tb.vhdl#L53-L63)：
   ```vhdl
   report "Vendor:            " & T_VENDOR'image(VENDOR)              severity note;
   report "Device:            " & T_DEVICE'image(DEVICE)             severity note;
   report "Device Family:     " & T_DEVICE_FAMILY'image(DEVICE_FAMILY) severity note;
   ```
3. （可选）用 GHDL/ModelSim 跑这个测试台；若无工具链，则纯做源码追踪。

**需要观察的现象**：注意 `config_tb.vhdl` 的断言（[tb/common/config_tb.vhdl:66-75](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/config_tb.vhdl#L66-L75)）目前断言的是 `VENDOR_GENERIC`/`DEVICE_GENERIC`，但旁边注释写的 `Expected=VENDOR_XILINX`、`Expected=DEVICE_KINTEX7`。这是因为该测试台默认配合 `my_config_GENERIC.vhdl` 跑，断言值与注释「期望」并不一致——这是个**已知的陈旧断言**，不要被它误导。

**预期结果**：若你真的把 `MY_DEVICE` 设成 `XC7K325T-2FFG900C` 并打开日志，`report` 行应打印 `Vendor: VENDOR_XILINX`、`Device: DEVICE_KINTEX7`。若只读源码，可对照 4.2.3(e) 的解析轨迹表自行确认。

#### 4.2.5 小练习与答案

**练习 1**：器件字符串 `EP4SGX230KF40C2`（Altera Stratix IV）会被解析成什么厂商？为什么 `VENDOR` 只看前 2 个字符就够？

**参考答案**：`EP…` 前缀 → `VENDOR_ALTERA`。`EP` 是 Altera 全系列通用的器件前缀，只用 2 字符就能和 Xilinx 的 `XC`、Generic 的 `GE` 区分开，因此放在第一个 `case VEN_STR2` 里优先命中，不必进入 3 字符分支。

**练习 2**：`getLocalDeviceString` 有三级优先（显式入参 > `MY_DEVICE` > `MY_BOARD`）。设计「显式入参」这一级是为了什么场景？

**参考答案**：为了让 `config` 的解析函数能被**复用**去解析任意一个器件字符串，而不只是全局的 `MY_DEVICE`。例如某个核想临时查询另一颗器件的属性，可以 `DEVICE_INFO("XC5VLX50T-1FF1136")` 直接传入，不必改动 `my_config`。默认不传参时才退化为「读全局配置」。

---

### 4.3 DEVICE_INFO 与 VENDOR 枚举：驱动厂商自动选择

#### 4.3.1 概念说明

前两节解析出的零散结果（`VENDOR`、`DEVICE`、`DEVICE_FAMILY`…）需要一个**统一的出口**给下游核使用。PoC 的做法是：定义一个记录类型 `T_DEVICE_INFO`，把所有属性打包成一个常量 `DEVICE_INFO`，下游核一次性拿到全部信息。

而 `VENDOR` 枚举（`VENDOR_ALTERA` / `VENDOR_XILINX` / `VENDOR_LATTICE` / `VENDOR_MICROSEMI` / `VENDOR_GENERIC` / `VENDOR_UNKNOWN`）是这条链上**最常被消费**的字段——它直接决定核走哪条厂商实现分支。这就是 u3-l2 将要讲的「可移植机制」的根基，本讲先看它如何被用到。

#### 4.3.2 核心流程

```text
        DEVICE_INFO(DeviceString)  ──返回──►  T_DEVICE_INFO 记录
                                                 │
              ┌──────────────────────────────────┤ 各字段
              ▼                                  ▼
        Vendor : T_VENDOR                DevSeries : T_DEVICE_SERIES
        Device : T_DEVICE                DevNumber : natural
        DevFamily : T_DEVICE_FAMILY      TransceiverType : T_TRANSCEIVER
        DevSubType : T_DEVICE_SUBTYPE    LUT_FanIn : positive
        DevGeneration : natural
                                                 │
                                                 ▼
        下游核:  constant DEV_INFO : T_DEVICE_INFO := DEVICE_INFO;
                 if DEV_INFO.Vendor = VENDOR_XILINX generate ... -- 选 Xilinx 实现
```

下游核的典型用法只有两步：① 在架构区声明一个 `constant DEV_INFO := DEVICE_INFO;`；② 在 `if generate` 里比较 `DEV_INFO.Vendor`。

#### 4.3.3 源码精读

**(a) `T_DEVICE_INFO` 记录**

记录定义见 [src/common/config.vhdl:520-531](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L520-L531)：

```vhdl
type T_DEVICE_INFO is record
  Vendor          : T_VENDOR;
  Device          : T_DEVICE;
  DevFamily       : T_DEVICE_FAMILY;
  DevGeneration   : natural;
  DevNumber       : natural;
  DevSubType      : T_DEVICE_SUBTYPE;
  DevSeries       : T_DEVICE_SERIES;
  TransceiverType : T_TRANSCEIVER;
  LUT_FanIn       : positive;        -- 该器件 LUT 的输入数（4 输入或 6 输入）
end record;
```

各字段含义：

| 字段 | 类型 | 含义 | 例（KC705） |
| --- | --- | --- | --- |
| `Vendor` | `T_VENDOR` | 厂商 | `VENDOR_XILINX` |
| `Device` | `T_DEVICE` | 具体器件型号（枚举） | `DEVICE_KINTEX7` |
| `DevFamily` | `T_DEVICE_FAMILY` | 器件家族 | `DEVICE_FAMILY_KINTEX` |
| `DevGeneration` | `natural` | 代数 | `7` |
| `DevNumber` | `natural` | 型号里的数字部分 | `325` |
| `DevSubType` | `T_DEVICE_SUBTYPE` | 子型号（如 `LXT`、`T`） | `DEVICE_SUBTYPE_T` |
| `DevSeries` | `T_DEVICE_SERIES` | 系列（7 系列 / UltraScale / …） | `DEVICE_SERIES_7_SERIES` |
| `TransceiverType` | `T_TRANSCEIVER` | 收发器类型 | `TRANSCEIVER_GTXE2` |
| `LUT_FanIn` | `positive` | LUT 输入宽度 | `6` |

**(b) `T_VENDOR` 枚举**

枚举定义见 [src/common/config.vhdl:390-397](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L390-L397)：

```vhdl
type T_VENDOR is (
  VENDOR_UNKNOWN,
  VENDOR_GENERIC,
  VENDOR_ALTERA,
  VENDOR_LATTICE,
  VENDOR_MICROSEMI,
  VENDOR_XILINX
);
```

类似地还有 `T_SYNTHESIS_TOOL`（[src/common/config.vhdl:401-409](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L401-L409)）、`T_DEVICE_FAMILY`（[src/common/config.vhdl:413-433](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L413-L433)）、`T_DEVICE`（[src/common/config.vhdl:446-470](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L446-L470)）等，构成了 PoC 对「FPGA 世界知识」的全部建模。

> 小插曲：`SYNTHESIS_TOOL` 函数对 Xilinx 的分支用了一个很奇怪的条件，见 [src/common/config.vhdl:796-801](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L796-L801)：`if (1 fs /= 1 us) then return ..._XST; else return ..._VIVADO;`。由于 `1 fs` 与 `1 us` 是不相等的时间值，该条件恒为真，实际总是返回 `SYNTHESIS_TOOL_XILINX_XST`，Vivado 分支看似不会被触发。这处逻辑是否在某工具链下表现不同，**待本地验证**——读源码时把它当成「区分 XST 与 Vivado 的占位尝试」即可。

**(c) `DEVICE_INFO` 汇总函数**

汇总就是把前面所有解析函数的结果填进记录，见 [src/common/config.vhdl:1162-1176](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L1162-L1176)：

```vhdl
function DEVICE_INFO(DeviceString : string := C_DEVICE_STRING_EMPTY) return T_DEVICE_INFO is
  variable Result : T_DEVICE_INFO;
begin
  Result.Vendor         := VENDOR(DeviceString);
  Result.Device         := DEVICE(DeviceString);
  Result.DevFamily      := DEVICE_FAMILY(DeviceString);
  Result.DevSubType     := DEVICE_SUBTYPE(DeviceString);
  Result.DevSeries      := DEVICE_SERIES(DeviceString);
  Result.DevGeneration  := DEVICE_GENERATION(DeviceString);
  Result.DevNumber      := DEVICE_NUMBER(DeviceString);
  Result.TransceiverType:= TRANSCEIVER_TYPE(DeviceString);
  Result.LUT_FanIn      := LUT_FANIN(DeviceString);
  return Result;
end function;
```

**(d) 下游消费：`sync_Bits` 的厂商自动选择**

这是「为什么要有这套机制」的最好例证。`sync_Bits` 先把 `DEVICE_INFO` 存成架构常量，见 [src/misc/sync/sync_Bits.vhdl:82-85](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L82-L85)：

```vhdl
architecture rtl of sync_Bits is
  constant INIT_I    : std_logic_vector := resize(descend(INIT), BITS);
  constant DEV_INFO : T_DEVICE_INFO     := DEVICE_INFO;   -- 一次性拿到全部器件信息
begin
```

然后用三个互斥的 `if generate` 选实现。通用分支见 [src/misc/sync/sync_Bits.vhdl:86](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L86)：

```vhdl
genGeneric : if ((DEV_INFO.Vendor /= VENDOR_ALTERA) and (DEV_INFO.Vendor /= VENDOR_XILINX)) generate
```

Altera 分支见 [src/misc/sync/sync_Bits.vhdl:119](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L119)：

```vhdl
genAltera : if (DEV_INFO.Vendor = VENDOR_ALTERA) generate
  sync : sync_Bits_Altera ...
```

Xilinx 分支见 [src/misc/sync/sync_Bits.vhdl:134](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L134)：

```vhdl
genXilinx : if (DEV_INFO.Vendor = VENDOR_XILINX) generate
  sync : sync_Bits_Xilinx ...
```

于是：用户在 `my_config` 里把 `MY_DEVICE` 从 `EP…` 换成 `XC…`，**源码一字不改**，综合时就自动从 `sync_Bits_Altera` 切到 `sync_Bits_Xilinx`。这就是 PoC 可移植性的底层引擎。

#### 4.3.4 代码实践

**实践目标**：以 KC705 为例，凭源码推导 `DEVICE_INFO` 记录的每一个字段值，验证理解。

**操作步骤**：

1. 设 `MY_BOARD := "KC705"`、`MY_DEVICE := "None"`（即 4.1 节看到的预置值）。
2. 因为 `MY_DEVICE="None"`，`getLocalDeviceString` 回退到 `MY_BOARD` → `BOARD("KC705")` 查表得到 `FPGADevice = "XC7K325T-2FFG900C"`（见 [src/common/config.vhdl:297-298](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L297-L298)）。
3. 对 `"XC7K325T-2FFG900C"` 依次套用：`VENDOR`、`DEVICE`、`DEVICE_FAMILY`、`DEVICE_NUMBER`、`DEVICE_SUBTYPE`、`DEVICE_SERIES`、`TRANSCEIVER_TYPE`、`LUT_FANIN`。
4. 填写下面这张表（先自己填，再对答案）：

| 字段 | 你推导的值 | 源码定位 |
| --- | --- | --- |
| `Vendor` | ? | `VENDOR`，[L762-781](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L762-L781) |
| `Device` | ? | `DEVICE` Xilinx 分支，[L845-859](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L845-L859) |
| `DevFamily` | ? | `DEVICE_FAMILY` Xilinx 分支，[L889-897](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L889-L897) |
| `DevNumber` | ? | `extractFirstNumber`，[L680-715](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L680-L715) |
| `DevSubType` | ? | `DEVICE_SUBTYPE` Kintex7 分支，[L1037-1040](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L1037-L1040) |
| `DevSeries` | ? | `DEVICE_SERIES`，[L905-924](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L905-L924) |
| `TransceiverType` | ? | `TRANSCEIVER_TYPE`，[L1090-1159](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L1090-L1159) |
| `LUT_FanIn` | ? | `LUT_FANIN`，[L1063-1088](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L1063-L1088) |

**需要观察的现象**：把每个字段的推导过程在源码里逐行对上，尤其注意「切片位置」（第 1~2 位、第 3~4 位、第 4 位）。

**预期结果**（答案）：

| 字段 | 值 |
| --- | --- |
| `Vendor` | `VENDOR_XILINX`（前缀 `XC`） |
| `Device` | `DEVICE_KINTEX7`（第 3~4 位 `7K`） |
| `DevFamily` | `DEVICE_FAMILY_KINTEX`（第 4 位 `K`） |
| `DevNumber` | `325`（`extractFirstNumber("325T-2FFG900C")`） |
| `DevSubType` | `DEVICE_SUBTYPE_T`（Kintex-7 分支命中字符串含 `'T'`） |
| `DevSeries` | `DEVICE_SERIES_7_SERIES`（Kintex7 属 7 系列） |
| `TransceiverType` | `TRANSCEIVER_GTXE2`（Kintex7 直接返回，[L1138](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L1138)） |
| `LUT_FanIn` | `6`（7 系列 LUT 为 6 输入，[L1070-1071](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L1070-L1071)） |

#### 4.3.5 小练习与答案

**练习 1**：如果用户既不设 `MY_DEVICE`（或填 `"None"`），`MY_BOARD` 又填了一个 `C_BOARD_INFO_LIST` 里没有的板名，会发生什么？

**参考答案**：`BOARD()` 函数（[src/common/config.vhdl:720-733](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L720-L733)）遍历整张表都找不到匹配名时，执行 `report "Unknown board name ..." severity failure;`，在确立阶段直接报错中止。这是「快速失败」设计——配置错误不应等到综合后期才暴露。

**练习 2**：`LUT_FanIn` 这个字段为什么也放进 `DEVICE_INFO`？它和厂商选择无关吧？

**参考答案**：虽然它不直接用于厂商分支选择，但某些核的算法实现依赖「LUT 是 4 输入还是 6 输入」来决定最优拆分方式（比如算术单元、宽逻辑函数）。把它和厂商等信息一起打包进 `DEVICE_INFO`，下游核就能一次性拿到所有「器件架构知识」，而不必各自重新解析器件字符串。这体现了「集中建模、统一消费」的设计思路。

---

## 5. 综合实践

把三个模块串起来，完成本讲规格要求的追踪任务：**把 `MY_DEVICE` 改成一个 Xilinx 型号，在 `config.vhdl` 中追踪它如何被识别为 `VENDOR_XILINX`，并描述 `DEVICE_INFO` 记录里包含哪些字段。**

**任务**：

1. **配置**：从模板复制出 `my_config.vhdl`，设：
   ```vhdl
   constant MY_BOARD  : string  := "Custom";
   constant MY_DEVICE : string  := "XC7K325T-2FFG900C";
   constant MY_VERBOSE: boolean := TRUE;
   ```
2. **入口追踪**：画出从 `MY_DEVICE` 到 `DEVICE_INFO` 的完整数据流图，标注每一站的源码行号。提示：`use PoC.my_config.all`（[L377](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L377)）→ `getLocalDeviceString`（[L657-678](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L657-L678)）→ `VENDOR`（[L762-781](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L762-L781)）→ `DEVICE_INFO`（[L1162-1176](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L1162-L1176)）。
3. **字段清点**：列出 `T_DEVICE_INFO` 的全部 9 个字段（[L520-531](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L520-L531)），并对照 4.3.4 的答案表填出每个字段对 `XC7K325T-2FFG900C` 的取值。
4. **消费验证**：打开 [src/misc/sync/sync_Bits.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl)，确认 `DEV_INFO.Vendor = VENDOR_XILINX` 时（[L134](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L134)）会实例化 `sync_Bits_Xilinx`，从而证明：仅靠改一个字符串常量，核的实现就被自动切换。
5. **换厂商对比**：把 `MY_DEVICE` 再改成 `EP4SGX230KF40C2`（Altera），重做第 3 步的关键字段（`Vendor`、`Device`、`DevFamily`），观察 `sync_Bits` 会改走 `genAltera` 分支。

**交付物**：一张「器件字符串 → 各解析函数 → `DEVICE_INFO` 各字段 → 下游分支」的对照表，以及一句结论：为什么 PoC 能做到「一份源码、多厂商可移植」。

> 若本地无 VHDL 工具链，第 4、5 步的运行效果标注「待本地验证」；仅做源码追踪同样能完成结论部分。

## 6. 本讲小结

- PoC 的配置分两个用户文件：`my_config.vhdl` 描述**目标硬件**（`MY_BOARD`/`MY_DEVICE`），`my_project.vhdl` 描述**宿主环境**（`MY_PROJECT_DIR`/`MY_OPERATING_SYSTEM`），二者都只是裸常量包。
- `config` 包靠 `use PoC.my_config.all` / `PoC.my_project.all` 读取这些常量；编译顺序由 `common.files` 保证（`my_config`/`my_project` 必须先于 `config`）。
- 解析的诀窍是**按器件型号字符串的位置切片 + 模式匹配**：前 2~3 字符定厂商（`XC`→Xilinx、`EP`→Altera…），第 3~4 位定器件，第 4 位定家族。
- 器件字符串有三級优先来源：显式入参 > `MY_DEVICE` > `MY_BOARD` 查表推断（`getLocalDeviceString`），`"None"` 表示「留空、回退」。
- 所有解析结果汇总进 `T_DEVICE_INFO` 记录（9 个字段：厂商、器件、家族、代数、型号数字、子型号、系列、收发器类型、LUT 输入数），由 `DEVICE_INFO()` 函数一次性产出。
- 下游核（如 `sync_Bits`）只需比较 `DEV_INFO.Vendor` 即可在 `if generate` 里自动选择厂商专用实现——这是 PoC 可移植性的底层引擎，也为 u3-l2 的「厂商选择与可移植机制」铺好了路。

## 7. 下一步学习建议

- **紧接着读 u3-l2（厂商选择与可移植机制）**：那里会系统讲解「通用包装实体 + 厂商专用子实体 + `if generate`」的三段式写法，本讲的 `sync_Bits` 就是它的第一个实例。
- **回头验证 u2-l2（utils 包）**：本讲里反复出现的 `ite`、`imin`、`bound` 都来自 `utils.vhdl`，结合那一讲能把这些辅助函数彻底吃透。
- **扩展阅读**：挑一个厂商专用子实体（如 `src/misc/sync/sync_Bits_Xilinx.vhdl`）看它如何利用 `DEVICE_INFO` 之外的器件特性（如 `ASYNC_REG` 属性），为 u4-l5（板级约束）做铺垫。
- **动手延伸**：尝试在 `C_BOARD_INFO_LIST` 里仿照现有条目（[src/common/config.vhdl:158-368](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L158-L368)）添加一块自己手头的开发板，体会「配置即数据表」的可扩展性。
