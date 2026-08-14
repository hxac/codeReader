# 运行第一个样例：DCMI 查询 NPU PCIe 信息

## 1. 本讲目标

学完本讲后，你应该能够：

- 看懂 `examples/dcmi` 样例的目录组织方式，并使用 `run.sh` 编译、运行一个 DCMI 样例。
- 理解 DCMI 查询类接口的「三段式」调用链：`dcmi_init` → `dcmi_get_card_num_list` → `dcmi_get_device_pcie_info`。
- 能够把样例代码 `main.c` 中的每一行，映射回 `dcmi_interface_api.h` 里声明的公共接口与结构体字段，从而具备「照着样例写自己的 DCMI 程序」的能力。

本讲是整个学习路线里第一次真正「跑代码」的讲义，目的是让你在脑子里建立起「上层应用一行 DCMI 调用 → 最终读到 NPU 硬件信息」的最短、最直观的路径。

## 2. 前置知识

本讲默认你已经学过：

- **u1-l1 三层架构**：知道 DCMI 是面向管理工具的最上层接口，实现位于 `custom` 源码树。
- **u1-l2 编译部署**：知道驱动如何编译、安装，以及 `libascend_hal.so` / `.ko` 的关系。
- **u1-l3 目录结构**：知道 `examples/` 是对外交付的样例目录，`pkg_inc/` 与 `src/custom/include/` 是公共头文件目录。

此外，几个通用概念需要先讲清楚，因为样例代码会直接用到：

- **卡（Card）与设备（Device）**：一张昇腾 NPU 板卡插在主机 PCIe 槽位上，称为一张「卡」，用 `card_id` 标识；一张卡上可能有多个 AI 芯片，每个芯片用一个 `device_id` 标识。最常见的板卡是「一卡一芯片」，此时 `device_id` 固定为 `0`。本讲的样例就只查询每张卡的 `device_id = 0`。
- **BDF（Bus / Device / Function）**：PCIe 总线用「总线号 : 设备号 : 功能号」三级地址定位一块硬件，简称 BDF。例如 `bdf_busid=0x81, bdf_deviceid=0x00, bdf_funcid=0x00` 对应 `0000:81:00.0` 这个 PCIe 地址。样例会把这三个字段分别打印出来。
- **动态库与链接**：DCMI 接口编译成一个动态库 `libdcmi.so`，用户态程序需要 `-ldcmi` 链接它，并用 `LD_LIBRARY_PATH` 指明库所在目录。

> 小提示：如果你手头没有真实 NPU 环境，本讲的所有「运行类」步骤都可以标注为「待本地验证」；「阅读/跟踪类」步骤在纯源码环境下也能完成。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `examples/dcmi/dcmi/1_query_npuinfo/0_get_pcie_info/main.c` | 本讲主角：查询并打印每张卡 `device 0` 的 PCIe 信息的最小 C 程序。 |
| `examples/dcmi/dcmi/1_query_npuinfo/1_get_board_info/main.c` | 结构几乎相同的姊妹样例，查询的是单板（board）信息，用于综合实践对照。 |
| `examples/dcmi/dcmi/run.sh` | 统一的编译运行脚本：用 `gcc` 编译 `main.c`，链接 `-ldcmi`，然后执行。 |
| `examples/README.md` / `examples/dcmi/README.md` | 样例使用指导：环境准备、目录与模块对应关系、运行命令。 |
| `examples/utils.h` | 样例公共工具宏（`LOG_ERR`、`CHECK_ERROR` 等），多个样例可复用。 |
| `src/custom/include/dcmi_interface_api.h` | DCMI 公共接口头文件：声明 `dcmi_init`、`dcmi_get_card_num_list`、`dcmi_get_device_pcie_info` 等函数，以及 `struct dcmi_pcie_info` 等结构体。 |

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：先认识样例目录与 `run.sh`，再依次精读三段式调用链中的三个函数。

### 4.1 examples/dcmi 样例目录组织与 run.sh 脚本

#### 4.1.1 概念说明

`examples/dcmi/dcmi/` 下并不是把所有样例平铺在一起，而是按「功能大类」分了三个主目录，每个主目录下再按「序号_功能名」分子目录，每个子目录里就是一个独立的 `main.c`：

```
examples/dcmi/dcmi/
├── 0_configure_manager/   # 配置类：用户配置、设备共享状态
│   ├── 0_set_user_config/main.c
│   └── 1_set_device_share/main.c
├── 1_query_npuinfo/        # 查询类：PCIE / Board / Flash 信息
│   ├── 0_get_pcie_info/main.c      ← 本讲主角
│   ├── 1_get_board_info/main.c
│   └── 2_get_flash_info/main.c
├── 2_chip_reset/           # 复位类：带内 / 带外热复位
│   ├── 0_internal_reset/main.c
│   └── 1_external_reset/main.c
└── run.sh                  # 统一的编译运行脚本
```

这套「主目录 → 子目录 → main.c」的三层结构，配合一个统一脚本 `run.sh`，让你不必为每个样例写各自的 Makefile，直接用 `bash run.sh <主目录别名> <子目录前缀>` 即可编译运行任意一个样例。

#### 4.1.2 核心流程

`run.sh` 的工作流程可以概括为三步：

1. **别名映射**：把你传入的短别名（如 `query`）映射到真实主目录（`1_query_npuinfo`）。
2. **前缀匹配**：在主目录下，按子目录前缀（如 `0`）匹配出要处理的子目录（`0_get_pcie_info`）。
3. **编译并运行**：进入匹配到的子目录，执行一条固定的 `gcc` 命令编译 `main.c`，成功后立即执行生成的 `main`。

#### 4.1.3 源码精读

**别名与主目录的映射表**，定义在 `run.sh` 顶部，`query` 就对应 `1_query_npuinfo`：

[examples/dcmi/dcmi/run.sh:11-16](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/examples/dcmi/dcmi/run.sh#L11-L16) —— 定义了 `user/query/reset/upgrade` 四个别名到真实目录的映射。

**编译命令**，是理解「样例如何链接 DCMI 库」的关键：

```bash
BUILD_CMD="gcc main.c -I/usr/local/dcmi/ -I${TOPDIR}/src/custom/include -I${TOPDIR}/pkg_inc \
           -L/usr/local/dcmi/ -L/usr/local/Ascend/driver/lib64/driver -ldcmi -o main"
```

[examples/dcmi/dcmi/run.sh:19](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/examples/dcmi/dcmi/run.sh#L19) —— 这条命令说明了三件事：
- `-I` 指定头文件搜索路径：既包含已安装环境的 `/usr/local/dcmi/`（提供 `dcmi_interface_api.h`），也包含仓库源码里的 `src/custom/include` 和 `pkg_inc`。
- `-L` 指定库搜索路径，`-ldcmi` 链接 `libdcmi.so` 动态库。
- 产物是一个名为 `main` 的可执行文件。

> 注意：`README` 提到 `dcmi_interface_api.h` 来自已安装驱动环境的 `/usr/local/dcmi/` 目录；仓库源码里的 `src/custom/include/dcmi_interface_api.h` 是它的源头，本讲引用的就是这份源头文件。

**运行方式**，在样例指导里写得很明确：

[examples/dcmi/README.md:46-51](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/examples/dcmi/README.md#L46-L51) —— 运行格式为 `bash run.sh <主目录别名> <子目录序号>`，例如 `bash run.sh query 0` 就会运行 `0_get_pcie_info`。

#### 4.1.4 代码实践

1. **实践目标**：亲手用 `run.sh` 编译并运行 `get_pcie_info` 样例。
2. **操作步骤**：
   ```bash
   cd ${git_clone_path}/examples/dcmi/dcmi
   export LD_LIBRARY_PATH=~/usr/local/dcmi/:$LD_LIBRARY_PATH   # 指明 dcmi 库路径
   bash run.sh query 0
   ```
3. **需要观察的现象**：脚本先打印「开始编译...」，成功后打印「开始运行程序...」，接着输出卡数量和 PCIe 信息。
4. **预期结果**：在装有真实 NPU 的机器上，会看到形如 `card count is 1` 以及一组 `deviceid / venderid / bdf_busid` 等十六进制字段。若机器上没有 NPU，`dcmi_init()` 会返回非 0 错误码，程序打印 `Failed to init dcmi.` 后退出。
5. **若无法确定运行结果**：明确写「待本地验证」——本实践依赖真实昇腾硬件与已安装的 `libdcmi.so`。

#### 4.1.5 小练习与答案

- **练习 1**：如果要运行 `1_get_board_info` 样例，应该执行什么命令？
  - **答案**：`bash run.sh query 1`。因为 `query` 映射到 `1_query_npuinfo` 主目录，前缀 `1` 匹配到 `1_get_board_info`。
- **练习 2**：`bash run.sh all` 会做什么？
  - **答案**：它会遍历所有主目录下、每个含 `main.c` 的子目录，依次编译并运行，最后汇总哪些样例成功、哪些失败（参考 `run.sh` 中的 `run_all_possible_params` 函数）。

---

### 4.2 dcmi_init：DCMI 初始化

#### 4.2.1 概念说明

在使用任何 DCMI 查询接口之前，必须先调用 `dcmi_init()` 完成初始化。它的职责是：建立与底层驱动/设备的通信通道、加载产品形态配置、完成必要的环境判断。可以把 `dcmi_init` 理解成 DCMI 库的「开机自检」——没通过自检，后面的查询接口都不会正常工作。

`dcmi_init` 是**无参数**的，返回一个 `int` 类型的错误码：返回 `0` 表示成功（样例里用宏 `NPU_OK` 代替 `0`），非 `0` 表示失败。

#### 4.2.2 核心流程

样例里调用 `dcmi_init` 的固定写法是「调用 → 判断返回值 → 失败则退出」：

```c
ret = dcmi_init();
if (ret != NPU_OK) {        // NPU_OK 即 0
    printf("Failed to init dcmi.\n");
    return ret;             // 把错误码原样返回，方便定位
}
```

这种「检查返回值」的防御式写法，是所有 DCMI 样例的通用范式，几乎每一个 DCMI 调用后面都跟着同样的判断。

#### 4.2.3 源码精读

**接口声明**位于 DCMI v2 接口段：

[src/custom/include/dcmi_interface_api.h:2001](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L2001) —— 声明 `DCMIDLLEXPORT int dcmi_init(void);`，无参，返回 `int`。

其中 `DCMIDLLEXPORT` 是一个导出宏，在 Linux 下展开为空、在 Windows 下展开为 `_declspec(dllexport)`，用来让同一份头文件跨平台编译：

[src/custom/include/dcmi_interface_api.h:23-27](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L23-L27) —— `DCMIDLLEXPORT` 的平台相关定义。

**样例中的实际调用**：

[examples/dcmi/dcmi/1_query_npuinfo/0_get_pcie_info/main.c:24-28](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/examples/dcmi/dcmi/1_query_npuinfo/0_get_pcie_info/main.c#L24-L28) —— `dcmi_init()` 调用及错误处理。

> 术语解释：`dcmi_init` 的真正实现位于 `src/custom/dev_prod/user/dcmi/dcmi_init/`（这是 u2-l1 会深入的内容）。本讲只需知道它是「一切查询的前提」。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：理解「不初始化会怎样」，加深对 `dcmi_init` 必要性的认识。
2. **操作步骤**：
   - 阅读样例 `main.c`，确认 `dcmi_init()` 是第一条 DCMI 调用。
   - 在本地拷贝一份 `main.c`，**注释掉** `dcmi_init();` 那一行（连同它的判断），重新用 `run.sh` 的同一条 `gcc` 命令编译运行。
3. **需要观察的现象**：直接调用 `dcmi_get_card_num_list` 时返回什么。
4. **预期结果**：未初始化时，后续查询接口大概率返回非 0 错误码（如通信未就绪）。**待本地验证**具体错误码数值。
5. 注意：本实践只是阅读 + 局部修改验证，不会改动仓库源码。

#### 4.2.5 小练习与答案

- **练习 1**：为什么样例用 `#define NPU_OK (0)` 而不直接写 `0`？
  - **答案**：用有名字的宏代替魔法数字 `0`，能让 `if (ret != NPU_OK)` 的语义「返回值不等于成功」一目了然，提升可读性。
- **练习 2**：`dcmi_init` 需要传卡号或设备号吗？
  - **答案**：不需要。`dcmi_init(void)` 是全局初始化，建立的是整个 DCMI 库的运行环境，与具体哪张卡无关。

---

### 4.3 dcmi_get_card_num_list：枚举系统中所有 NPU 卡

#### 4.3.1 概念说明

初始化之后，第一个要回答的问题是：「这台机器上一共有几张 NPU 卡？分别是哪些 `card_id`？」这正是 `dcmi_get_card_num_list` 的职责。它一次性返回「卡的数量」和「卡号列表」两份数据。

这个函数是后续所有「逐卡查询」的前置步骤——你必须先拿到 `card_id` 列表，才能在循环里逐张卡去查询 PCIe / Board / Flash 等信息。

#### 4.3.2 核心流程

调用模式是「传入一个数组，函数往里填卡号，并用一个出参告诉你填了几个」：

```
dcmi_get_card_num_list(&card_count, card_id_list, MAX_CARD_NUM)
        │              │                │              │
        │              │                │              └── 数组容量（最多放 64 个）
        │              │                └── 输出：卡号列表数组（函数往里写）
        │              └── 输出：实际卡数量（函数往里写）
        └── 返回值：0 成功，非 0 失败
```

用数学符号描述：设系统有 \( n \) 张卡（\( 0 \le n \le 64 \)），调用成功后满足
\[ \text{card\_count} = n, \quad \{\text{card\_id\_list}[0], \dots, \text{card\_id\_list}[n-1]\} \text{ 为全部有效卡号} \]

#### 4.3.3 源码精读

**`MAX_CARD_NUM` 容量上限**：

[src/custom/include/dcmi_interface_api.h:31](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L31) —— `#define MAX_CARD_NUM 64`，系统最多支持 64 张卡，所以样例里 `int card_id_list[MAX_CARD_NUM]` 开了 64 个槽位。

**接口声明**：

[src/custom/include/dcmi_interface_api.h:3613](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L3613) —— `int dcmi_get_card_num_list(int *card_num, int *card_list, int list_len);`，三个参数分别是：出参卡数量、出参卡号数组、数组容量。

**样例中的实际调用**：

[examples/dcmi/dcmi/1_query_npuinfo/0_get_pcie_info/main.c:20-34](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/examples/dcmi/dcmi/1_query_npuinfo/0_get_pcie_info/main.c#L20-L34) —— 声明 `card_count` 与 `card_id_list`，调用 `dcmi_get_card_num_list`，并打印卡数量。

注意第 21 行 `int card_id_list[MAX_CARD_NUM] = {0};` 把数组初始化为全 0，这是一种好习惯，避免读到未初始化内存。

#### 4.3.4 代码实践（跟踪型）

1. **实践目标**：跟踪 `card_count` 与 `card_id_list` 在程序里的数据流动。
2. **操作步骤**：
   - 在样例第 34 行 `printf("card count is %d \n", card_count);` 之后，再加一行：`for (int k=0;k<card_count;k++) printf("card_id_list[%d]=%d\n", k, card_id_list[k]);`。
   - 重新编译运行。
3. **需要观察的现象**：卡号列表里的具体数值。
4. **预期结果**：在单卡机器上，`card_count` 为 `1`，`card_id_list[0]` 通常为 `0`；多卡机器则会列出多个卡号。**待本地验证**具体数值。
5. 这一实践帮助你看清：第 4.4 节循环里 `card_id_list[i]` 取到的正是这里填进来的卡号。

#### 4.3.5 小练习与答案

- **练习 1**：`dcmi_get_card_num_list` 的第三个参数 `list_len` 有什么用？
  - **答案**：它告诉函数「我提供的数组最多能放几个卡号」，防止函数写入越界。样例传入 `MAX_CARD_NUM`（64），与数组容量一致。
- **练习 2**：如果一台机器装了 70 张卡（超过 64），这个接口会怎样？
  - **答案**：受 `MAX_CARD_NUM = 64` 限制，数组最多容纳 64 个卡号，超出的无法装入；实际系统设计上也不会超过该上限。这是一种典型的「定长数组 + 容量参数」防越界设计。

---

### 4.4 dcmi_get_device_pcie_info：查询设备 PCIe 信息

#### 4.4.1 概念说明

拿到卡号后，就可以查询每张卡上某个设备的硬件信息了。`dcmi_get_device_pcie_info` 专门查询 PCIe 相关的标识：厂商 ID、设备 ID、子厂商/子设备 ID，以及用于在 PCIe 总线上定位这块卡的 BDF 三元组。

- **厂商 ID（venderid）/ 设备 ID（deviceid）**：PCIe 标准里用来标识「这块卡是谁家、什么型号」的两个数字，操作系统（如 `lspci`）也是靠它们识别设备的。
- **子厂商 / 子设备 ID**：板卡制造层面的细分标识（对应插在主板上的模组）。
- **BDF（bus / device / function）**：PCIe 总线地址，用来唯一定位这块卡在主机里的物理位置。

#### 4.4.2 核心流程

查询接口的固定调用模式是「指定卡号 + 设备号 → 函数把结果填进结构体」：

```
for (i = 0 .. card_count-1):
    dcmi_get_device_pcie_info(card_id_list[i], device_id=0, &pcie_info)
            │                    │              │
            │                    │              └── 输出：结果写入 pcie_info 结构体
            │                    └── 查询哪个设备（样例固定查 device 0）
            └── 查询哪张卡
    打印 pcie_info.deviceid / venderid / bdf_* 等字段
```

为什么样例里 `device_id` 固定为 `0`？因为最常见的板卡是「一卡一芯片」，每张卡上只有 `device 0` 这一个 AI 芯片。如果你面对的是多芯片封装的卡，则需要遍历 `0..device_num-1`（可用 `dcmi_get_device_num_in_card` 获取每卡设备数）。

#### 4.4.3 源码精读

**结果结构体**：

[src/custom/include/dcmi_interface_api.h:89-97](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L89-L97) —— `struct dcmi_pcie_info`，包含 7 个字段：`deviceid`、`venderid`、`subvenderid`、`subdeviceid`、`bdf_deviceid`、`bdf_busid`、`bdf_funcid`。

注意：还有一个增强版结构体 `struct dcmi_pcie_info_all`（同文件第 99-109 行），额外携带 `domain` 字段，由 `dcmi_get_device_pcie_info_v2` 填充，适用于需要 PCIe domain 的场景。

**接口声明**：

[src/custom/include/dcmi_interface_api.h:2019](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L2019) —— `int dcmi_get_device_pcie_info(int card_id, int device_id, struct dcmi_pcie_info *pcie_info);`。

**样例中的查询循环与字段打印**（本讲核心代码）：

```c
for (int i = 0; i < card_count; i++) {
    ret = dcmi_get_device_pcie_info(card_id_list[i], device_id, &pcie_info);
    if (ret != NPU_OK) { ... return ret; }
    printf("设备ID (deviceid):     0x%08X\n", pcie_info.deviceid);
    printf("厂商ID (venderid):     0x%08X\n", pcie_info.venderid);
    printf("BDF-总线ID (bdf_busid): 0x%02X\n", pcie_info.bdf_busid);
    /* ...其余字段同理... */
}
```

[examples/dcmi/dcmi/1_query_npuinfo/0_get_pcie_info/main.c:35-49](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/examples/dcmi/dcmi/1_query_npuinfo/0_get_pcie_info/main.c#L35-L49) —— 逐卡查询并打印每个字段。结构体字段名与打印标签一一对应，体现了「接口结构体 → 打印输出」的直接映射。

> 字段含义对照：
> | 结构体字段 | 含义 | 典型用途 |
> |---|---|---|
> | `deviceid` | PCIe 设备 ID | 识别芯片型号 |
> | `venderid` | PCIe 厂商 ID | 识别厂商（如华为） |
> | `subvenderid` / `subdeviceid` | 子厂商 / 子设备 ID | 板卡模组级标识 |
> | `bdf_busid` / `bdf_deviceid` / `bdf_funcid` | BDF 三元组 | PCIe 总线定位 |

#### 4.4.4 代码实践（跟踪型）

1. **实践目标**：把样例输出与 `lspci` 的输出对上号，理解 BDF 的物理含义。
2. **操作步骤**：
   - 运行样例，记下某张卡的 `bdf_busid / bdf_deviceid / bdf_funcid` 三个十六进制值。
   - 在主机上执行 `lspci | grep -i <venderid 对应的厂商关键字>`，找到同一块卡，看它的 PCI 地址是否与样例 BDF 拼接结果一致（地址格式为 `domain:bus:device.function`，样例省略了 domain）。
3. **需要观察的现象**：样例 BDF 与 `lspci` 地址的对应关系。
4. **预期结果**：例如样例打印 `bdf_busid=0x81, bdf_deviceid=0x00, bdf_funcid=0x00`，则在 `lspci` 里能找到形如 `81:00.0` 的设备条目。**待本地验证**。
5. 这个实践让你把抽象的「BDF 字段」与操作系统里看得见的 PCIe 地址联系起来。

#### 4.4.5 小练习与答案

- **练习 1**：样例里为什么用 `0x%08X` 打印 `deviceid`，却用 `0x%02X` 打印 `bdf_busid`？
  - **答案**：`deviceid` 是 32 位（`unsigned int`），用 8 位十六进制显示完整；`bdf_busid` 实际只用低 8 位表示总线号，用 2 位十六进制显示更贴合其取值范围，便于与 `lspci` 输出对照。
- **练习 2**：如果一台卡上有 2 个 AI 芯片，样例能查到第 2 个芯片的 PCIe 信息吗？
  - **答案**：不能。样例把 `device_id` 硬编码为 `0`，只查每张卡的 `device 0`。要查全部芯片，需先用 `dcmi_get_device_num_in_card` 拿到每卡设备数，再嵌套一层 `device_id` 循环。

---

## 5. 综合实践

把本讲的「三段式」调用链用到一个新的查询接口上，检验你是否真正掌握了样例的套路。

**任务**：参照 `0_get_pcie_info/main.c` 的结构，改写出一个查询并打印每张卡单板（board）信息的新样例。

**操作步骤**：

1. 复制 `examples/dcmi/dcmi/1_query_npuinfo/0_get_pcie_info/main.c`，在其同级或 `1_get_board_info` 目录下新建一个 `main.c`（仓库已有 `1_get_board_info` 样例可作参考答案对照）。
2. 把结果变量从 `struct dcmi_pcie_info pcie_info` 改为 `struct dcmi_board_info_stru board_info`（见下方结构体）。
3. 把查询调用从 `dcmi_get_device_pcie_info(...)` 改为 `dcmi_get_board_info(card_id_list[i], device_id, &board_info)`。
4. 按结构体字段打印：`board_id`、`pcb_id`、`bom_id`、`slot_id`。
5. 用 `bash run.sh query 1` 编译运行（若你把文件放在了 `1_get_board_info` 目录）。

**关键结构体（你需要在头文件里查到的字段含义）**：

[src/custom/include/dcmi_interface_api.h:3436-3441](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L3436-L3441) —— `struct dcmi_board_info_stru`，4 个字段：

| 字段 | 含义 |
|------|------|
| `board_id` | 单板 ID，标识这块板卡的型号 |
| `pcb_id` | PCB（印制电路板）版本编号 |
| `bom_id` | BOM（物料清单）版本编号 |
| `slot_id` | 槽位号，标识板卡插在哪个物理槽位 |

**对应接口声明**：

[src/custom/include/dcmi_interface_api.h:3617](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L3617) —— `int dcmi_get_board_info(int card_id, int device_id, struct dcmi_board_info_stru *board_info);`

**参考答案骨架**（示例代码，非仓库原有文件）：

```c
#include <stdio.h>
#include "dcmi_interface_api.h"
#define NPU_OK (0)
int main(void)
{
    int ret;
    int card_count = 0;
    int card_id_list[MAX_CARD_NUM] = {0};
    int device_id = 0;
    struct dcmi_board_info_stru board_info = {0};

    ret = dcmi_init();
    if (ret != NPU_OK) { printf("Failed to init dcmi.\n"); return ret; }

    ret = dcmi_get_card_num_list(&card_count, card_id_list, MAX_CARD_NUM);
    if (ret != NPU_OK) { printf("Failed to get card number.\n"); return ret; }

    for (int i = 0; i < card_count; i++) {
        ret = dcmi_get_board_info(card_id_list[i], device_id, &board_info);
        if (ret != NPU_OK) { printf("Failed to get board info. ret=%d\n", ret); return ret; }
        printf("board_id=0x%08X pcb_id=0x%08X bom_id=0x%08X slot_id=0x%02X\n",
               board_info.board_id, board_info.pcb_id, board_info.bom_id, board_info.slot_id);
    }
    return ret;
}
```

> 完成后，你可以直接对比仓库自带的 [examples/dcmi/dcmi/1_query_npuinfo/1_get_board_info/main.c](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/examples/dcmi/dcmi/1_query_npuinfo/1_get_board_info/main.c) 验证自己的写法是否一致。运行结果**待本地验证**。

这个综合实践能让你体会到：DCMI 的查询类样例几乎都是同一套「`dcmi_init` → 枚举卡 → 循环查某类信息」的模板，换一个接口、换一个结构体，就是一个新功能。

## 6. 本讲小结

- `examples/dcmi/dcmi/` 按「功能大类 → 序号子目录 → main.c」三级组织，用一个统一的 `run.sh` 用 `bash run.sh <别名> <前缀>` 编译运行任意样例。
- DCMI 查询类程序遵循固定**三段式**：`dcmi_init()` 初始化 → `dcmi_get_card_num_list()` 枚举卡 → 循环调用具体查询接口（如 `dcmi_get_device_pcie_info`）。
- 每个接口都返回 `int` 错误码，`0` 为成功；样例用 `NPU_OK` 宏代替 `0`，且每个调用后都做返回值检查。
- `card_id` 标识卡、`device_id` 标识卡内芯片；样例固定查 `device 0`，因为多数板卡是「一卡一芯片」。
- `struct dcmi_pcie_info` 的 7 个字段（deviceid / venderid / subvenderid / subdeviceid / BDF 三元组）与 PCIe 标准标识一一对应，可与 `lspci` 输出对照理解。
- 样例代码与 `dcmi_interface_api.h` 中的接口声明、结构体定义是直接映射关系，学会查头文件就能照着写出新的查询程序。

## 7. 下一步学习建议

- **横向扩展**：阅读 `2_get_flash_info/main.c`，它展示了「卡循环 + 设备内 flash 循环」的**双层循环**查询模式，比本讲的单层循环更复杂，是向多芯片场景过渡的练习。
- **纵向深入**：进入 **u2-l1（DCMI 接口总览与初始化流程）**，去 `src/custom/dev_prod/user/dcmi/` 看 `dcmi_init` 的真正实现，弄清 `dcmi_environment_judge` 等模块在初始化里做了哪些环境与产品形态判断。
- **对照内核侧**：学完 u2 单元后，可结合 **u3 单元（HAL 层与 HDC 通信）** 理解这些 DCMI 查询最终是如何经 DSMI、HDC、ioctl 一路下沉到内核态 `.ko` 再读到硬件寄存器的——也就是把本讲这条「最短路径」补全成「完整跨层路径」。
