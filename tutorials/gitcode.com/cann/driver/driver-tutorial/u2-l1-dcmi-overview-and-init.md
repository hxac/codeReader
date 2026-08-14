# DCMI 接口总览与初始化流程

## 1. 本讲目标

本讲是「DCMI 设备管理接口层」单元的第一篇。在 [u1-l5](u1-l5-public-headers-and-api.md) 里我们已经知道：DCMI 是面向管理工具的定制化接口层，头文件位于 `src/custom/include/dcmi_interface_api.h`，以「卡 + 设备」两级定位设备，错误码使用 `-8000` 偏移体系。但头文件只声明了接口，**实现到底放在哪、`dcmi_init()` 第一次被调用时内部做了什么**，是本讲要回答的问题。

学完后你应当能够：

- 说清 DCMI 实现在 `src/custom/dev_prod/user/dcmi/` 下的三层目录划分：`dcmi_interface` / `dcmi_inner` / `dcmi_init`，以及它们各自的职责。
- 画出 `dcmi_init()` 的内部调用顺序，理解它如何按板型（MODEL / CARD / SERVER / SOC）分阶段初始化。
- 解释 `dcmi_environment_judge.c` 在初始化里扮演的「环境探测」角色，以及它如何影响后续接口行为。

---

## 2. 前置知识

在进入源码前，先建立几个直觉概念。

### 2.1 为什么 DCMI 需要一个「初始化」过程

DCMI 面向管理工具（如 `npu-smi`、运维脚本），这些程序启动后第一件事几乎都是 `dcmi_init()`。原因在于：同一份 DCMI 库要跑在非常多形态的硬件和软件环境上——

- **硬件形态不同**：同一颗昇腾芯片可能做成 PCIe 加速卡（CARD）、整机服务器（SERVER）、边缘小站/型号机（MODEL）、或片上系统（SOC，即 RC 模式，芯片自己就是主机）。不同形态下「一张卡有几个芯片」「怎么获取板卡 ID」「板卡槽位号怎么算」都不一样。
- **运行环境不同**：DCMI 可能运行在物理机、虚拟机、普通容器、特权容器里。在容器/虚拟机里，很多底层信息（如 PCIe 拓扑、MCU 信息）拿不到，必须走不同分支。

所以 `dcmi_init()` 的核心使命是：**在第一次调用时，把这些「形态」和「环境」探测清楚，缓存到一个全局结构体里，后续所有查询接口都直接读这份缓存，而不必每次都去问设备。** 这就是为什么本讲会反复出现全局变量 `g_board_details` 和 `g_run_env`。

### 2.2 两个关键全局变量

| 全局变量 | 定义位置 | 缓存内容 | 谁来填充 |
|---|---|---|---|
| `g_board_details` | `dcmi_init_basic.c` | 板型、子板型、芯片型号、产品型号、卡/设备列表、PCIe 信息 | `dcmi_init` 阶段的 `dcmi_init_for_*` 系列 |
| `g_run_env` | `dcmi_environment_judge.c` | 是否 root、是否虚拟机、是否容器、是否已初始化 | `dcmi_run_env_init()` |

理解了「初始化 = 探测并填缓存」，后面的源码就一目了然了。

### 2.3 名词速查

- **RC / EP 模式**：RC（Root Complex，根复合体，值为 0）指芯片本身就是主机（SOC 场景）；EP（Endpoint，端点，值为 1）指芯片作为 PCIe 设备插在主机上（卡/服务器场景）。定义见 [dcmi_inner_info_get.h:52-L53](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_inner/inc/dcmi_inner_info_get.h#L52-L53)。
- **板型 board_type**：MODEL（型号机）/ CARD（加速卡）/ SERVER（服务器）/ SOC（片上系统）/ INVALID（未知）。
- **DCMI_VERSION_2**：当前头文件同时定义了 V1 和 V2，实际编译走 V2 分支（见下文）。

---

## 3. 本讲源码地图

本讲涉及的关键文件与作用：

| 文件 | 所属子模块 | 作用 |
|---|---|---|
| `src/custom/include/dcmi_interface_api.h` | 对外头文件 | 声明 `dcmi_init` / `dcmiv2_init` 等对外接口 |
| `src/custom/dev_prod/user/dcmi/dcmi_init/src/dcmi_init_basic.c` | dcmi_init | 初始化主链路：`dcmi_init` / `dcmi_board_init` / `dcmi_init_board_type`，以及 `chip_info_table` 表 |
| `src/custom/dev_prod/user/dcmi/dcmi_init/src/dcmi_init_for_card.c` | dcmi_init | CARD 板型的初始化实现（本讲作为分阶段示例） |
| `src/custom/dev_prod/user/dcmi/dcmi_init/inc/dcmi_init_basic.h` | dcmi_init | `ChipInfo` 表项结构与 `dcmi_init_for_*` 声明 |
| `src/custom/dev_prod/user/dcmi/dcmi_inner/src/dcmi_environment_judge.c` | dcmi_inner | 运行环境探测：root / vm / docker 判定与 ENV 标志位计算 |
| `src/custom/dev_prod/user/dcmi/dcmi_inner/inc/dcmi_environment_judge.h` | dcmi_inner | `struct dcmi_run_env` 与环境判定接口声明 |

---

## 4. 核心概念与源码讲解

本讲围绕三个最小模块展开：**dcmi_interface**（接口门面）、**dcmi_inner**（内部支撑）、**dcmi_init**（初始化引擎）。

### 4.1 DCMI 的三层目录划分：interface / inner / init

#### 4.1.1 概念说明

DCMI 的实现不是堆在一个目录里，而是按「职责」拆成了三个并列子目录，都位于 `src/custom/dev_prod/user/dcmi/` 下。这是阅读 DCMI 源码首先要建立的地图：

- **`dcmi_interface/`**：对外接口的实现层。每一个对外暴露的 `dcmi_*` / `dcmiv2_*` 接口，其函数体基本都落在这里（如 `dcmi_basic_info_intf.c`、`dcmi_hot_reset_intf.c`、`dcmi_network_intf.c` 等）。它做参数校验、产品形态适配，然后转调内部能力。这是 DCMI 的「门面」。
- **`dcmi_inner/`**：内部公共能力层。存放被多个 interface 复用的辅助逻辑：环境判定（`dcmi_environment_judge.c`）、信息获取（`dcmi_inner_info_get.c`）、产品判定（`dcmi_product_judge.c`）、权限判定（`dcmi_permission_judge.c`）、日志（`dcmi_log.c`）、I2C/SMBus 操作等。它不直接面向用户，但几乎所有 interface 都依赖它。
- **`dcmi_init/`**：初始化引擎。负责 `dcmi_init()` 的全部流程，包括主控 `dcmi_init_basic.c` 和按板型分的 `dcmi_init_for_card.c` / `dcmi_init_for_model.c` / `dcmi_init_for_server.c` / `dcmi_init_for_soc.c`。

#### 4.1.2 核心流程

这三层是「调用关系」，不是「包含关系」：

```
   用户态程序（npu-smi 等）
            │  调用
            ▼
   dcmi_interface/   ← 门面：dcmi_init / dcmi_get_xxx 实现
            │  转调
            ▼
   dcmi_init/        ← 引擎：探测板型、环境，填充 g_board_details
   dcmi_inner/       ← 支撑：环境判定、信息获取、权限、日志
            │  最终经 dsmi_* / ioctl 下沉到 HAL/内核
            ▼
        设备 / 内核驱动
```

值得注意的一点：对外接口 `dcmi_init` 声明在 `dcmi_interface_api.h`（interface 的头文件），但**实现**却在 `dcmi_init/dcmi_init_basic.c`（init 模块）。这种「声明与实现跨目录」在 DCMI 里很常见，定位代码时要以实际目录为准。

#### 4.1.3 源码精读

先看对外接口的声明。`dcmi_init` 和 `dcmiv2_init` 都受 `DCMI_VERSION_2` 宏控制导出：

[dcmi_interface_api.h:1996-L2014](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/include/dcmi_interface_api.h#L1996-L2014) —— 头文件同时定义 V1、V2，并在 `#if defined DCMI_VERSION_2` 下导出 `dcmi_init`、`dcmi_get_dcmi_version` 等接口。`DCMIDLLEXPORT` 是跨平台导出宏（Linux 上展开为可见符号），`int` 返回值，成功为 0。

再确认三个子目录各自的文件构成（下表即 `ls` 结果的归类）：

| 子目录 | 代表文件 | 数量特征 |
|---|---|---|
| `dcmi_interface/src/` | `dcmi_basic_info_intf.c`、`dcmi_hot_reset_intf.c`、`dcmi_network_intf.c`、`dcmi_running_info_intf.c` …，以及 `dcmiv2_*.c` 系列约 13 个 | 文件最多，每个文件对应一类对外能力 |
| `dcmi_inner/src/` | `dcmi_environment_judge.c`、`dcmi_inner_info_get.c`、`dcmi_common.c`、`dcmi_product_judge.c`、`dcmi_permission_judge.c`、`dcmi_log.c`、`dcmi_i2c_operate.c` … | 公共支撑，被多处复用 |
| `dcmi_init/src/` | `dcmi_init_basic.c` + `dcmi_init_for_{card,model,server,soc}.c` 共 5 个 | 数量最少，职责最聚焦 |

可以看到，`dcmiv2_*` 系列接口（如 `dcmiv2_basic_info_intf.c`、`dcmiv2_network_intf.c`）是面向 ascend950（A5）场景的新版实现，与 `dcmi_*` 一一对应；这与后续 `dcmiv2_init` 仅支持 A5 的设定一致。

#### 4.1.4 代码实践

1. **实践目标**：建立 DCMI 源码的「目录—职责」心智地图，能在源码里快速定位一个接口。
2. **操作步骤**：
   - 进入 `src/custom/dev_prod/user/dcmi/`，分别列出三个子目录的 `src/` 内容。
   - 选一个对外接口，例如 `dcmi_get_device_pcie_info`（[u1-l4](u1-l4-first-dcmi-example.md) 用过），用 `grep` 在三个目录里找它的实现所在文件。
3. **需要观察的现象**：对外查询接口的实现几乎都落在 `dcmi_interface/src/` 下；而初始化相关的函数（`dcmi_init`、`dcmi_init_for_card` 等）落在 `dcmi_init/src/` 下。
4. **预期结果**：`dcmi_get_device_pcie_info` 的实现位于 `dcmi_interface/src/dcmi_basic_info_intf.c`（板卡基础信息类），而 `dcmi_init` 的实现位于 `dcmi_init/src/dcmi_init_basic.c`。
5. 若本地未编译、无法运行，可仅做源码检索，此为「源码阅读型实践」。

#### 4.1.5 小练习与答案

**练习 1**：`dcmi_init` 声明在 interface 的头文件里，为什么实现却在 init 模块？  
**参考答案**：DCMI 按职责拆目录。`dcmi_init` 是对外接口（属于 interface 门面范畴），所以声明在 `dcmi_interface_api.h`；但它的函数体本质是「初始化引擎」逻辑，按代码归属放在 `dcmi_init/src/dcmi_init_basic.c`，由该模块编译产出。声明与实现跨目录是 DCMI 的常态。

**练习 2**：`dcmiv2_network_intf.c` 与 `dcmi_network_intf.c` 是什么关系？  
**参考答案**：`dcmiv2_*` 是面向 ascend950（A5）场景的新版接口实现，与同名 `dcmi_*` 一一对应。两套并存是为了在 A5 与传统芯片上分别走不同实现路径。

---

### 4.2 dcmi_init 初始化主链路：dcmi_init 模块

#### 4.2.1 概念说明

`dcmi_init()` 是 DCMI 的「总入口」。它无参数、返回 `int`（0 为成功）。它的职责不是「打开设备」（设备由更下层管理），而是**做一次性探测，把板型和环境信息缓存进全局变量**，然后把 `init_flag` 置位，表示「后续接口可以放心读缓存了」。

它最巧妙的设计是**按板型分派**：先用一张表识别出当前是哪种板型（CARD/SERVER/MODEL/SOC），再调用对应的 `dcmi_init_for_*` 函数做该形态专属的初始化。这样每个形态的复杂逻辑互不干扰。

#### 4.2.2 核心流程

`dcmi_init()` 的主干可分为 6 个阶段（对应源码自上而下的顺序）：

```
dcmi_init()
  │
  ├─① dcmi_run_env_init()         // 探测 root/vm/docker，填 g_run_env
  │
  ├─② 形态校验（非装备构建下，950 走 dcmiv2_init，这里直接拒绝）
  │     dcmi_check_chip_type_is_ascend_950()
  │
  ├─③ dcmi_cfg_create_lock_dir() ×7  // 创建各类锁目录
  │
  ├─④ dcmi_board_init()           // 【核心】识别板型 + 分派初始化
  │     ├─ dcmi_get_npu_device_list()      // 枚举设备
  │     ├─ dcmi_init_board_type()          // 识别板型（chip_info_table）
  │     └─ switch(board_type):
  │           MODEL  → dcmi_init_for_model()
  │           CARD   → dcmi_init_for_card()
  │           SERVER → dcmi_init_for_server()
  │           SOC    → dcmi_init_for_soc()
  │
  ├─⑤ dcmi_init_ok()              // g_run_env.init_flag = TRUE
  │
  └─⑥ 收尾（非 station/hilens 时）
        dcmi_flush_device_id()      // 补全 mcu/cpu/槽位 id
        dcmi_pcie_slot_map_init()   // 建立 PCIe 槽位映射
        dcmi_card_info_sort()       // 卡/设备排序
```

`dcmiv2_init()` 的结构与 `dcmi_init()` 几乎一致，唯一区别在第②步：它**只**支持 A5 场景，若 `dcmi_check_chip_type_is_ascend_950()` 不成功就直接返回错误。这就是「V1 通用入口」与「V2 专用入口」的分工。

#### 4.2.3 源码精读

先看 `dcmi_init` 全貌：

[dcmi_init_basic.c:1439-L1488](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_init/src/dcmi_init_basic.c#L1439-L1488) —— 这就是上面流程图的源码本体。要点：

- 开头 `dcmi_run_env_init()` 是环境探测入口（见 4.3 节）。
- `#ifndef ENABLE_EQUIPMENT` 包裹的 `dcmi_check_chip_type_is_ascend_950()` 是一个「形态路由」：在非装备（equipment）构建里，若检测到是 950，说明用户应该改调 `dcmiv2_init`，于是复位环境值并返回 `DCMI_ERR_CODE_NOT_SUPPORT`。
- 中间连续 7 个 `dcmi_cfg_create_lock_dir()` 创建 vnpu/syslog/custom_op 等锁目录，是后续配置持久化的基础设施。
- 收尾阶段用 `dcmi_board_type_is_station() || dcmi_board_type_is_hilens()` 判断：station（开发者小站）和 hilens 形态不需要槽位映射等收尾，跳过即可。

再看「核心」`dcmi_board_init()`：

[dcmi_init_basic.c:1251-L1292](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_init/src/dcmi_init_basic.c#L1251-L1292) —— 它做三件事：

1. `dcmi_get_npu_device_list()` 拿到设备逻辑 ID 列表和数量；
2. `dcmi_init_board_type()` 识别板型（把 `g_board_details.board_type` 填好）；
3. `switch (dcmi_get_board_type())` 按 MODEL/CARD/SERVER/SOC 分派到对应的 `dcmi_init_for_*`。

注意这个 switch 的设计：**四种板型各走一条独立初始化路径**，default 分支返回 `DCMI_ERR_CODE_INNER_ERR`。这意味着识别不出板型时初始化直接失败——因为后续接口无处读缓存。

最后看分派的「调度员」`dcmi_init_board_type()`：

[dcmi_init_basic.c:743-L790](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_init/src/dcmi_init_basic.c#L743-L790) —— 它先 `dcmi_init_board_details_default()` 把全局结构清零成 INVALID，再尝试 `dcmi_get_board_type_by_config()`（针对 310 型号机从配置文件读板型）。然后通过 `dcmi_get_rc_ep_mode(&mode)` 判断 RC/EP，循环每个设备调用 `dcmi_get_boot_status` + `dcmi_get_board_info_handle` 拿到板卡信息。最后按 `mode` 分两路：

- RC 模式（SOC）：`dcmi_init_chip_board_product_for_rc()`；
- EP 模式（卡/服务器）：`dcmi_init_chip_board_product_for_ep()`，内部再用 `chip_info_table` 精确识别（见 4.3 节）。

为了体会「按板型分派」的具体样子，看 CARD 分支的实现入口：

[dcmi_init_for_card.c:875-L892](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_init/src/dcmi_init_for_card.c#L875-L892) —— `dcmi_init_for_card()` 记录设备总数后，核心是 `dcmi_get_card_id_from_bus_id()`：它通过解析 `/sys/devices/pci*` 下的 PCIe 拓扑，把每个逻辑设备归并到正确的「卡」上（一卡多芯片场景），再 `dcmi_flush_pcie_device()` 统计掉设备（用于上报丢失的芯片），最后标记是否有 MCU、NPU。这正是一张加速卡场景专属的逻辑，与服务器/SOC 完全不同。

#### 4.2.4 代码实践（本讲主实践）

> 对应规格中的实践任务：阅读 `dcmi_init_basic.c`，画出 `dcmi_init` 的内部调用顺序，并说明 `dcmi_environment_judge.c` 的作用。

1. **实践目标**：亲手把 `dcmi_init` 的调用链画出来，并标注每个阶段读/写了哪个全局变量。
2. **操作步骤**：
   - 打开 [dcmi_init_basic.c:1439-L1488](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_init/src/dcmi_init_basic.c#L1439-L1488)。
   - 逐行标注 6 个阶段（见 4.2.2 流程图），用箭头连出调用关系。
   - 对每个 `dcmi_init_for_*`，打开对应文件看它最终填充了 `g_board_details` 的哪些字段（如 `card_count`、`device_count`、`card_info[]`、`is_has_mcu`）。
3. **需要观察的现象**：`dcmi_init` 本身很短（约 50 行），真正的复杂度被下沉到了 `dcmi_board_init` 和四个 `dcmi_init_for_*` 里；主函数只负责「编排」。
4. **预期结果**：得到一张以 `dcmi_init` 为根、以 `dcmi_init_for_{model,card,server,soc}` 为四个分支的调用树，每个叶子节点都指向对 `g_board_details` 的写入。
5. 若无法在真实环境运行，本实践为「源码阅读 + 画图」型，结论可直接从源码得出，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`dcmi_init` 第②步为什么要在 `#ifndef ENABLE_EQUIPMENT` 里检查 950？  
**参考答案**：ascend950（A5）有专用的 `dcmiv2_init` 入口。在非装备（普通交付）构建里，如果用户误对 950 调了 `dcmi_init`，这里会探测到 950 并返回 `DCMI_ERR_CODE_NOT_SUPPORT`，提示改用 `dcmiv2_init`；装备构建（`ENABLE_EQUIPMENT`）下则允许 `dcmi_init` 处理 950，故用宏隔开。

**练习 2**：为什么 station/hilens 形态要跳过第⑥步收尾？  
**参考答案**：station（开发者小站）和 hilens 是单芯片的型号机/SOC 形态，不存在「多卡 PCIe 槽位映射」「MCU/CPU id 补全」这类多板卡需求，`dcmi_flush_device_id` / `dcmi_pcie_slot_map_init` 对它们无意义，故跳过。

---

### 4.3 产品/板型识别与环境判断：dcmi_inner 的支撑能力

#### 4.3.1 概念说明

`dcmi_init` 能正确分派，依赖两个 `dcmi_inner` 提供的关键能力：

1. **环境探测**（`dcmi_environment_judge.c`）：在初始化最开始判断「我跑在什么环境」，结果存入 `g_run_env`，并在运行期提供更精确的 `ENV_*` 标志位给权限判定使用。
2. **产品/板型识别**（`chip_info_table` + `dcmi_inner_info_get.c`）：通过 PCIe 厂商/设备 ID 查表，确定芯片型号，再据此推断板型和产品型号。

本节聚焦**环境探测**（它正是实践任务要求解释的 `dcmi_environment_judge.c`），并顺带讲清板型识别的「表驱动」设计。

#### 4.3.2 核心流程

环境探测分轻重两套：

```
【轻量探测：初始化时一次性完成】
dcmi_init() → dcmi_run_env_init()
   ├─ dcmi_is_not_root_user()       // geteuid() != 0 ?
   ├─ dcmi_is_in_virtual_machine()  // dmidecode / systemd-detect-virt
   ├─ dcmi_is_in_docker()           // /.dockerenv + cgroup + mount
   └─ dcmi_set_env_value(...)       // 存入 g_run_env

【精确探测：运行期按需调用】
dcmi_get_environment_flag()
   ├─ devdrv_get_host_phy_mach_flag()   // 问设备：宿主是不是物理机
   ├─ dmanage_get_container_flag()      // 是否普通容器
   └─ dcmi_determine_environment_flag() // 组合成 ENV_* 枚举
```

`g_run_env` 的三个布尔位（`is_not_root` / `is_in_vm` / `is_in_docker`）组合出一系列便捷判定函数（如 `dcmi_is_in_phy_machine_root`、`dcmi_is_in_vm_root`），被各 interface 用来决定「这个接口在当前环境能不能用、怎么用」。

而板型识别用的是「表驱动 + 函数指针」：

```
dcmi_init_chip_board_product_type(pcie_id_info)
   └─ 遍历 chip_info_table[]
        匹配 (venderid, deviceid) → 调用对应的 init_board_type() / init_product_type()
```

#### 4.3.3 源码精读

先看 `dcmi_run_env_init()` 与全局结构：

[dcmi_init_basic.c:32-L43](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_init/src/dcmi_init_basic.c#L32-L43) —— `dcmi_run_env_init()` 调三个判定函数后用 `dcmi_set_env_value()` 把结果写入 `g_run_env`；`dcmi_init_ok()` 则把 `init_flag` 置 `TRUE`，标记初始化完成。

[dcmi_environment_judge.h:26-L31](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_inner/inc/dcmi_environment_judge.h#L26-L31) —— `struct dcmi_run_env` 只有四个 int：`init_flag` + 三个环境布尔位。非常精简。

再看三个判定函数的真身（都在 `dcmi_environment_judge.c`）：

- [dcmi_environment_judge.c:160-L184](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_inner/src/dcmi_environment_judge.c#L160-L184) —— `dcmi_is_in_virtual_machine()`：依次尝试 `dmidecode` 匹配 xen/VMware/KVM、QEMU 厂商标识、`systemd-detect-virt -v`，任一命中即认为在虚拟机。
- [dcmi_environment_judge.c:275-L290](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_inner/src/dcmi_environment_judge.c#L275-L290) —— `dcmi_is_in_docker()`：先查文件（`/.dockerenv`、`/proc/self/cgroup` 里的 `docker-`/`docker/`），再查命令（`systemd-detect-virt -c`、`mount` 根分区是否 isulad/docker/containerd）。

精确探测的「决策矩阵」在：

[dcmi_environment_judge.c:94-L121](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_inner/src/dcmi_environment_judge.c#L94-L121) —— `dcmi_determine_environment_flag()` 用两个维度组合出 `ENV_*` 标志：横轴是「物理机 / 虚拟机」（由设备返回的 `host_flag` 是否等于 `DCMI_HOST_PHY_MACH_FLAG` 决定），纵轴是「普通容器 / 特权容器 / 非容器」。结果枚举（`ENV_PHYSICAL`、`ENV_VIRTUAL`、`ENV_PHYSICAL_PRIVILEGED_CONTAINER` 等）定义在 `dcmi_common.h`，例如 `ENV_PHYSICAL = 1`、`ENV_VIRTUAL = 4`。

> 这套 `ENV_*` 标志与 `g_run_env` 的三个布尔位是**两套并行机制**：前者精确（直接问设备）、用于权限判定；后者轻量（只看本机）、用于初始化期的快速分支。不要混淆。

最后看板型识别的「表」：

[dcmi_init_basic.c:624-L662](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_init/src/dcmi_init_basic.c#L624-L662) —— `chip_info_table[]` 是一张静态表，每项是 `ChipInfo{venderid, deviceid, chip_type, init_board_type, init_product_type}`。`dcmi_init_chip_board_product_type()` 遍历此表，用 PCIe 的 `(venderid, deviceid)` 匹配，命中后通过**函数指针**调用该项专属的板型/产品型号初始化函数。`ChipInfo` 结构定义见 [dcmi_init_basic.h:16-L22](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_init/inc/dcmi_init_basic.h#L16-L22)。

这种「表驱动 + 函数指针」的好处是：**新增一颗芯片只需在表里加一行，并实现两个回调函数**，而不必改 `dcmi_init_chip_board_product_type` 的匹配逻辑。这是 DCMI 应对多芯片适配的核心模式。

#### 4.3.4 代码实践

1. **实践目标**：理解 `dcmi_environment_judge.c` 在初始化中起的「前置探测」作用。
2. **操作步骤**：
   - 读 [dcmi_environment_judge.c:150-L184](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_inner/src/dcmi_environment_judge.c#L150-L184)，列出 `dcmi_is_not_root_user` / `dcmi_is_in_virtual_machine` 各用了哪些系统手段。
   - 用 `grep` 在 `dcmi_init_for_card.c` 里搜索 `dcmi_check_run_in_vm` / `dcmi_check_run_in_docker`，看它们如何影响 CARD 初始化（例如 PCIe 拓扑查找命令在 VM/容器里换成简化版）。
3. **需要观察的现象**：`g_run_env` 的布尔位在初始化最早期就被填好，后续 `dcmi_init_for_*` 多处根据它切换分支。
4. **预期结果**：能用自己的话说出——`dcmi_environment_judge.c` 的作用是「在一切设备交互之前，先用本机手段探明运行环境，把结果缓存到 `g_run_env`，让后续初始化和接口能针对物理机/虚拟机/容器走不同路径」。
5. 本实践为源码阅读型；若想验证行为，可在容器内运行任意调用 `dcmi_init` 的管理工具并用 `strace` 观察其对 `dmidecode`/`/proc/self/cgroup` 的访问（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`g_run_env` 的三个布尔位与 `dcmi_get_environment_flag()` 返回的 `ENV_*` 有何区别？  
**参考答案**：前者是轻量本机探测（看 `geteuid`、`dmidecode`、`/.dockerenv` 等），在初始化早期一次性填好，用于快速分支；后者是精确探测，需要向设备查询 `host_phy_mach_flag` 和容器标志后组合得出，用于权限判定。两套机制并行存在。

**练习 2**：要支持一颗新芯片，`chip_info_table` 模式需要改哪些地方？  
**参考答案**：在 `chip_info_table[]` 加一行（填厂商/设备 ID、chip_type、两个回调函数指针），并实现该行专属的 `init_board_type` 与 `init_product_type` 两个回调。匹配主逻辑 `dcmi_init_chip_board_product_type` 无需改动——这就是表驱动的可扩展性。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一个「初始化链路追踪」任务：

**任务**：假设你正在 review 一份新增 DCMI 接口的代码，需要确认它依赖的初始化前提是否满足。请完成以下分析：

1. **定位声明与实现**：指出 `dcmi_init` 的声明文件（interface 门面）与实现文件（init 引擎），说明这种跨目录归属的合理性。
2. **追踪 `dcmi_init` 的 6 个阶段**：对照 [dcmi_init_basic.c:1439-L1488](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_init/src/dcmi_init_basic.c#L1439-L1488)，写出每个阶段调用的函数名，并标注它读/写的是 `g_board_details` 还是 `g_run_env`。
3. **解释环境探测的前置作用**：说明若把第①步 `dcmi_run_env_init()` 删掉，后续 `dcmi_init_for_card()` 里哪些分支会出错（提示：PCIe 拓扑查找命令的选择依赖 `dcmi_check_run_in_vm/dcmi_check_run_in_docker`）。
4. **画出板型分派树**：以 `dcmi_board_init` 为根，画出 MODEL/CARD/SERVER/SOC 四条分支，并在 CARD 分支上展开到 `dcmi_init_for_card` → `dcmi_get_card_id_from_bus_id`。

**交付物**：一张 `dcmi_init` 调用树图 + 一段说明「为什么新增 DCMI 接口前通常要先 `dcmi_init`」（因为接口实现直接读 `g_board_details` / `g_run_env` 缓存，未初始化则这些全局变量是 INVALID/0）。

> 提示：可在 [dcmi_init_for_card.c:141-L171](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/custom/dev_prod/user/dcmi/dcmi_init/src/dcmi_init_for_card.c#L141-L171) 看到 PCIe 查找命令 `mat_string_vm` 与 `mat_string` 的二选一，这正是受 `g_run_env` 影响的典型分支。

---

## 6. 本讲小结

- DCMI 实现按职责拆成三层目录：`dcmi_interface`（对外接口门面）、`dcmi_inner`（环境/信息/权限等内部支撑）、`dcmi_init`（初始化引擎）。
- `dcmi_init()` 的本质是「一次性探测 + 填缓存」：探测板型与环境，写入全局 `g_board_details` 与 `g_run_env`，最后置 `init_flag`。
- 初始化分 6 个阶段，核心是 `dcmi_board_init()`：先用 `chip_info_table`（表驱动 + 函数指针）识别芯片/板型，再按 MODEL/CARD/SERVER/SOC 分派到 `dcmi_init_for_*`。
- `dcmi_environment_judge.c` 在初始化最早期用本机手段（`geteuid`/`dmidecode`/`/.dockerenv` 等）探明 root/vm/docker，缓存到 `g_run_env`，供后续分支使用；另有精确的 `ENV_*` 标志用于权限判定。
- `dcmiv2_init()` 是 ascend950（A5）专用入口，结构与 `dcmi_init` 几乎一致，但只接受 950 形态。
- 表驱动设计让新增芯片只需加一行表项 + 两个回调，是 DCMI 多芯片适配的核心模式。

---

## 7. 下一步学习建议

本讲讲清了 DCMI 的「骨架与初始化」。接下来建议：

- **u2-l2 DSMI 设备系统管理接口实现**：DCMI 的很多查询最终转调 `dsmi_*`（如本讲反复出现的 `dsmi_get_board_info`、`dsmi_get_chip_info`、`dsmi_get_pcie_info_v2`）。下一讲深入 DSMI 的实现套路，理解 DCMI 是如何「搭」在 DSMI 之上的。
- **u2-l4 DCMI 查询与复位接口实现**：如果想看 `dcmi_interface/src/` 里具体接口怎么写（参数校验 → 转调 inner → 返回错误码），可直接跳到这篇，对照本讲的目录地图阅读 `dcmi_basic_info_intf.c`。
- 建议同时翻开 `dcmi_inner/src/dcmi_inner_info_get.c` 与 `dcmi_product_judge.c`，它们是本讲多次引用却未展开的两个 inner 支撑文件，理解它们能补全「板型/产品是怎么一步步问出来的」这条链路。
