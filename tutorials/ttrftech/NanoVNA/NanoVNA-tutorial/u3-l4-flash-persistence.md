# flash.c：配置与校准槽的掉电保存

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 NanoVNA 为什么不用文件系统，而是「按固定地址直接读写结构体」来保存配置与校准数据。
2. 画出 128K Flash 的完整地址地图：96K 程序区 + 32K 保存区（1 页 config + 5 个校准槽），并解释 0x1800 槽位间距的由来。
3. 读懂 STM32F0 flash 控制器的三件底层操作：解锁（KEYR）、页擦除（PER/AR/STRT）、半字编程（PG），以及 BSY 忙等待。
4. 理解 `magic + checksum` 两级数据有效性校验链分别挡住哪两类损坏。
5. 掌握 `config_save/config_recall` 与 `caldata_save/caldata_recall` 的完整流程，包括校准槽读失败的默认值回退路径。

## 2. 前置知识

### 2.1 Flash 和 RAM 的本质区别

RAM 读写自由、掉电即失；Flash 掉电保持，但写入受三条物理约束支配（这两条是理解本讲全部代码的钥匙）：

- **擦除按「页」进行**：要把某单元改写，必须先擦除它所在的整页（STM32F072 一页 2KB）。擦除后整页变成全 `0xFF`。
- **编程只能把 1 改成 0**：写入时任何二进制位只能从 1 变 0，不能从 0 变 1。想把 0 改回 1，唯一办法是整页重新擦除。
- **最小编程单位是半字（16 位）**：STM32F0 的 flash 控制器一次接受 16 位数据，不是单字节。

所以「改一个参数存档」在 flash 上的真实动作是：**擦除整页 → 按半字逐个写回**。本讲的 `caldata_save` 就是这样做的。

### 2.2 为什么嵌入式固件经常「绕过文件系统」

NanoVNA 没有 SD 卡、没有 littlefs/FatFS，也没有磨损管理。它采用最朴素的方案：**把 C 结构体原样字节拷贝到约定的 flash 地址**，读的时候先验证再拷回 RAM。数据结构就是「文件格式」，地址就是「文件名」。代价与收益：

- 代价：改一个字节也要重擦整页；没有掉电原子性保证（写一半断电数据就废了）。
- 收益：零依赖、代码极短（整个 flash.c 不到 230 行）、读写速度完全可预期。

### 2.3 magic 与 checksum：两级「这数据还能信吗」检查

- **magic（魔数）**：结构体第一个字段写入一个约好的常量 `0x434f4e45`（按大端序读作 `'CONF'`）。读出时如果不等于它，说明这块 flash 从未写过（擦除态读出全 `0xFF`）或格式根本不对，直接判废。
- **checksum（校验和）**：对结构体除 checksum 字段外的全部字节计算一个 32 位摘要，与存档里的值比对，挡住「写入被打断」「位翻转」这类局部损坏。

### 2.4 与前几讲的衔接

- u3-l2 中 `cal_collect` 把标准件测量快照 memcpy 进 `cal_data`——那个数组就住在本讲的 `properties_t` 里；
- u3-l3 中反复出现的 `active_props` 双指针（指向 flash 槽或指向 SRAM 工作副本 `current_props`），正是在 `caldata_recall/caldata_save` 里被切换的；
- u1-l2 讲过链接脚本把 32K 划为 `.calsave`（NOLOAD）段、DFU 升级不碰它——本讲来看这 32K 内部的精确布局。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [flash.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c) | 本讲主角：flash 底层操作 + config/校准槽的读写回退 + 全区擦除，共约 230 行 |
| [nanovna.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h) | 定义 `config_t`、`properties_t` 两个存档结构与全部 SAVE_* 地址宏 |
| [STM32F072xB.ld](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/STM32F072xB.ld) | 链接脚本：96K 程序区 + 32K 保存区的内存划分 |
| [main.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c) | 调用方：`cmd_save/cmd_recall/cmd_saveconfig/cmd_clearconfig` 四个 shell 命令、开机恢复序列、`load_default_properties` |
| [ui.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c) | 调用方：屏幕菜单上的 SAVE/RECALL 回调 |
| [plot.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c) | 消费 `lastsaveid`：屏幕角落 `C*` / `C0`~`C4` 标记的来源 |

## 4. 核心概念与源码讲解

### 4.1 flash 页擦除与半字编程

#### 4.1.1 概念说明

STM32 的 flash 控制器是一台独立的「小状态机」，CPU 不能像写 RAM 那样直接 `*addr = value` 写 flash。要操作它必须按参考手册规定的寄存器序列来：

| 寄存器 | 名称 | 用途 |
|---|---|---|
| `FLASH->KEYR` | 密钥寄存器 | 依次写入两个魔数解锁控制器，否则写操作被硬件拒绝 |
| `FLASH->CR` | 控制寄存器 | `PG` 位=允许编程、`PER` 位=允许页擦除、`STRT` 位=启动擦除 |
| `FLASH->AR` | 地址寄存器 | 存放要擦除的页地址 |
| `FLASH->SR` | 状态寄存器 | `BSY` 位=控制器忙 |

flash.c 把这四类寄存器的操作封装成 5 个小函数，是全项目唯一直接触碰 flash 控制器的地方。

#### 4.1.2 核心流程

解锁（一次即可）：

```
flash_unlock():
    KEYR ← 0x45670123
    KEYR ← 0xCDEF89AB      # 连续写对两个魔数才解锁，防止程序跑飞误写
```

页擦除（擦哪页、怎么等）：

```
flash_erase_page(addr):
    关中断/进临界区 chSysLock()
    等 BSY 清零
    CR 置 PER 位            # 声明"接下来是页擦除"
    AR  ← addr              # 擦这一页
    CR 置 STRT 位           # 启动！硬件开始忙
    等 BSY 清零             # 擦一页耗时毫秒量级（以数据手册为准）
    CR 清 PER 位
    开临界区 chSysUnlock()
```

半字编程（写数据）：

```
flash_program_half_word(addr, data):
    等 BSY 清零
    CR 置 PG 位             # 声明"接下来是编程"
    *(uint16_t*)addr = data # 对 flash 地址做一次普通的 16 位存储访问，
                            # 硬件把它翻译成一次编程操作
    等 BSY 清零
    CR 清 PG 位
```

注意「置 PG → 写地址 → 等 BSY」这个顺序：**先挂号，再写数据**。直接写而不置 PG，总线会拒绝甚至触发硬错误。

#### 4.1.3 源码精读

忙等待，注意它用 `==` 整体比较而不是按位与 `&`（这是个值得玩味的写法，见练习 3）：

[flash.c:L25-L31](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L25-L31) —— `flash_wait_for_last_operation`：死循环读 `FLASH->SR`，只要它恰好等于 `FLASH_SR_BSY`（即只有忙位置位）就继续等，返回最终状态。

[flash.c:L33-L41](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L33-L41) —— `flash_erase_page0`：裸擦除序列「置 PER → 写 AR → 置 STRT → 等忙 → 清 PER」，与上面伪代码一一对应。

[flash.c:L43-L49](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L43-L49) —— `flash_erase_page`：用 `chSysLock()/chSysUnlock()` 临界区包住擦除。页擦除耗时毫秒量级，在 Cortex-M0 的 ChibiOS 移植里 `chSysLock()` 会关闭可屏蔽中断——也就是说**擦除期间整个系统对中断无响应**（音频 DMA 回调也会被推迟）。结合代码可以推断：这正是为什么保存动作只由用户显式触发（菜单 SAVE / shell 命令），而不在扫频热路径上自动存档。

[flash.c:L51-L58](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L51-L58) —— `flash_program_half_word`：半字编程。写入方式就是把 flash 地址强转成 `uint16_t*` 做一次存储访问（`*(__IO uint16_t*)address = data`），但前后必须用 PG 位「宣布」。

[flash.c:L60-L65](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L60-L65) —— `flash_unlock`：向 KEYR 依次写 `0x45670123`、`0xCDEF89AB` 两个出厂约定魔数。这两个数字来自 STM32F0 参考手册，没有别的含义，纯粹是「口令」。

[flash.c:L67-L76](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L67-L76) —— `checksum`：本讲的数据完整性核心。以 32 位字为单位，循环做「循环右移 31 位再加当前字」：

\[ v_{i+1} = \mathrm{ROR}(v_i,\,31) + w_i \]

在 32 位机上循环右移 31 位等价于循环左移 1 位。相比朴素累加 \(\sum w_i\)，这个旋转让**每个字因其出现位置不同而贡献到不同的比特位**——交换两个字、或在错误偏移上读出的数据，都会得到不同的校验和。这一位置敏感性正是「校验存档有没有写坏」所需要的性质。

#### 4.1.4 代码实践

**实践名称：在 PC 上模拟「擦除全 1、编程只能 1→0」的 flash 物理特性**（纯 PC 实践，无需硬件）

1. **实践目标**：亲眼看到「不擦除就重写会得到 AND 结果」，从而理解 `caldata_save` 为什么必须先擦 3 页再写。
2. **操作步骤**：新建 `flashsim.py`：

   ```python
   # 示例代码：单页 flash 行为模拟器
   PAGE = 0x800
   flash = bytearray(b'\xff' * PAGE)   # 擦除态 = 全 1

   def erase_page():
       for i in range(PAGE):
           flash[i] = 0xFF

   def program_half_word(addr, data):
       old = flash[addr] | (flash[addr+1] << 8)
       new = old & data                # 编程只能把 1 变 0
       flash[addr]   = new & 0xFF
       flash[addr+1] = new >> 8
       return old

   print(hex(program_half_word(0, 0x1234)))   # 第 1 次写
   print(hex(flash[0] | flash[1] << 8))       # 读回 0x1234
   print(hex(program_half_word(0, 0x5678)))   # 不擦除直接改写
   print(hex(flash[0] | flash[1] << 8))       # 读回是多少？
   erase_page()
   program_half_word(0, 0x5678)
   print(hex(flash[0] | flash[1] << 8))       # 擦除后写入才正确
   ```

3. **需要观察的现象**：第二次写 `0x5678` 后读回的不是 `0x5678`，而是 `0x1234 & 0x5678 = 0x1230`。
4. **预期结果**：五次输出依次为 `0xffff`（旧值全 1）、`0x1234`、`0x1234`、`0x1230`、`0x5678`。第三、四个输出证明「1→0 不可逆」；最后两个证明「先擦后写」才是正确顺序——这正是 `caldata_save` 的擦除循环存在的理由。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `flash_program_half_word` 每次只写 16 位，而不是 32 位一次写完省一半循环？

答案：STM32F0 的 flash 控制器以**半字（16 位）为最小编程单位**（见参考手册的编程时序），这是硬件规定。所以 [flash.c:L83-L105](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L83-L105) 里 `config_save` 把源、目的指针都转成 `uint16_t*`，循环 `sizeof(config_t)/2` 次。

**练习 2**：`flash_erase_page` 用临界区包住整个擦除，而 `flash_program_half_word` 完全没加锁，这样安全吗？

答案：擦除序列「置 PER → 写 AR → 置 STRT」必须连续完成，中途若被其他上下文插入别的 flash 寄存器操作，控制器状态就会被搅乱，且擦除耗时毫秒级、被中断打断的窗口大，故用 `chSysLock` 保护；单次半字编程只需微秒量级，代码选择不加锁。严格说两个函数若被两个线程并发调用仍可能互相干扰——但本固件的所有 save 都由单一用户动作触发（shell 的 sweep 线程或 UI 回调），实际上不会并发，这是「靠调用纪律而非锁保证安全」的又一处体现（呼应 u2-l5 的单写者纪律）。

**练习 3**：`while (FLASH->SR == FLASH_SR_BSY)` 与 `while (FLASH->SR & FLASH_SR_BSY)` 有什么差别？前者有什么隐患？

答案：`==` 要求状态寄存器**恰好只有** BSY 一位置位才继续等待；若 BSY 与其他位（例如错误标志）同时置位，循环会立刻退出，等于没等完就开始下一步操作。`&` 写法只关心 BSY 位，更稳健。当前代码在正常操作中 SR 通常确实只有 BSY 置位，所以能工作，但这是依赖了「无错误标志并存」的隐含假设。

### 4.2 config_save / config_recall：全局配置的存取

#### 4.2.1 概念说明

`config_t` 保存的是**与测量无关的全局偏好**：DAC 背光值、网格与菜单颜色、4 条轨迹颜色、触摸校准系数、谐波模式阈值、电池电压偏移。它独占保存区的第一页（0x08018000），与校准数据分离——换校准槽不需要动它，`clearconfig` 则会把它一起清掉。

它的「出厂默认值」不在任何 save 函数里，而在 main.c 的编译期初始化器中：`config` 是 `.data` 段的全局变量，C 运行时在启动阶段就把这些初值从 flash 装载进 RAM。所以 `config_recall` 失败时**什么都不用做**，RAM 里的 `config` 自动就是合理默认。

#### 4.2.2 核心流程

保存（写方向，RAM → flash）：

```
config_save():
    config.magic    = CONFIG_MAGIC                    # 盖章
    config.checksum = checksum(除 checksum 外的全部字段)  # 算摘要
    flash_unlock()
    擦除 0x08018000 所在的一页
    把 &config 按 uint16_t 逐半字写入该页
```

恢复（读方向，flash → RAM，带两级校验）：

```
config_recall():
    把 0x08018000 处当作 config_t* 读
    if magic   != CONFIG_MAGIC:      return -1        # 从未存过 / 格式不对
    if checksum(数据) != 存的 checksum: return -1        # 写坏 / 写一半断电
    memcpy(&config, flash地址, sizeof)                  # 校验通过才拷贝
    return 0
```

关键点：**校验在 flash 原地做，通过后才 memcpy**。任何一步失败，RAM 里的 `config` 保持编译期默认值不动——这就是「失败回退」。

#### 4.2.3 源码精读

先看数据结构。[nanovna.h:L225-L237](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L225-L237) 定义 `config_t`：`magic` 打头、`checksum` 殿后，中间是颜色/触摸/阈值等字段，末尾 `_reserved[22]` 留白。按 32 位 ARM 的自然对齐手算，各字段偏移为 magic@0、dac_value@4、grid_color@6、menu_normal_color@8、menu_active_color@10、trace_color[4]@12、touch_cal[4]@20、harmonic_freq_threshold@28、vbat_offset@32、_reserved@34、checksum@0x38，总计 **60 字节（0x3C）**，远小于一页 2KB。

[nanovna.h:L389](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L389) —— `CONFIG_MAGIC 0x434f4e45`，注释标明按 `'CONF'` 读。注意它被 `config_t` 和 `properties_t` **共用**：见到这个魔数只说明「这是一份 NanoVNA 存档」，不区分种类。

[flash.c:L79-L81](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L79-L81) —— 页大小 `FLASH_PAGESIZE 0x800`（2KB）与保存区起始 `save_config_area = SAVE_CONFIG_ADDR`（0x08018000），这两个常量是后面所有地址循环的步进。

[flash.c:L83-L105](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L83-L105) —— `config_save`：注意第 90-91 行先盖章、算校验和再动 flash；第 96 行只擦一页；第 99-102 行的写入循环把 `uint16_t*` 源指针逐半字搬到目的地址，`count = sizeof(config_t)/2 = 30` 次。

[flash.c:L107-L121](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L107-L121) —— `config_recall`：两级校验的精确写法。第 115 行 `checksum(src, sizeof *src - sizeof src->checksum)` 明确排除了 checksum 字段自身——摘要不能参与自证。第 119 行 memcpy 前的注释点明意图：把数据复制到 SRAM 才能修改。

[main.c:L788-L799](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L788-L799) —— `config` 的编译期默认值初始化器：dac_value=1922、默认颜色宏、2.8 寸屏的 touch_cal、谐波阈值 300MHz、vbat_offset=500。recall 失败时这些值就是生效配置。

[main.c:L557-L563](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L557-L563) 与 [main.c:L565-L580](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L565-L580) —— 两个 shell 入口：`saveconfig` 直接调 `config_save`；`clearconfig` 要求参数等于保护键 `"1234"` 才执行全区擦除，防误触。

#### 4.2.4 代码实践

**实践名称：用 Python 复现 rotate 校验和，验证它比朴素求和更敏锐**（纯 PC 实践）

1. **实践目标**：亲手验证 [flash.c:L67-L76](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L67-L76) 的校验和算法具有「位置敏感」性质。
2. **操作步骤**：新建 `checksum_demo.py`：

   ```python
   # 示例代码：flash.c checksum() 的 Python 复现
   import struct
   MASK = 0xFFFFFFFF

   def checksum(data: bytes) -> int:
       v = 0
       for (w,) in struct.iter_unpack('<I', data):   # 小端 32 位字
           v = ((v << 1) | (v >> 31)) & MASK          # ROR(v,31) == ROL(v,1)
           v = (v + w) & MASK
       return v

   words = [0x11223344, 0x55667788, 0x9A0B1C2D]
   blob = struct.pack('<3I', *words)
   swapped = struct.pack('<3I', words[1], words[0], words[2])  # 只交换前两个字

   print(hex(checksum(blob)))
   print(hex(checksum(swapped)))
   print(hex(sum(words) & MASK))   # 对照：朴素求和无法区分上面两者
   ```

3. **需要观察的现象**：交换两个字之后，rotate 校验和改变，而朴素求和不变。
4. **预期结果**：前两行输出不同（待本地验证具体数值），第三行对两种排列给出相同的值——这正是固件选择带旋转的算法而非 `sum` 的理由。
5. 进阶（可选，需真机）：在 USB shell 里执行 `color 3 0x03E0` 换轨迹颜色 → `saveconfig` → 拔电重启，颜色保持；再执行 `clearconfig 1234` 后手动复位，颜色回到默认——完整走一遍 save/recall/擦除三条路径。

#### 4.2.5 小练习与答案

**练习 1**：`config_recall` 的两次 if 分别挡住什么场景？

答案：`magic` 不符挡住「这块 flash 从未保存过（擦除态读出全 0xFF，magic 为 0xFFFFFFFF）」以及「存的是别的东西」；checksum 不符挡住「保存过程被打断（写了一半）」或「存储位翻转」。前者是粗筛（一次比较），后者是细查（遍历全部数据）。

**练习 2**：`config_recall` 失败返回 -1 后，为什么 `config.dac_value` 等仍有合理取值，而不是随机数？

答案：`config` 是带初始化器的全局变量，位于 `.data` 段，C 启动代码在进入 `main` 前已把初值（[main.c:L788-L799](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L788-L799)）从 flash 拷到 RAM；失败路径根本不执行 memcpy，所以编译期默认值原样保留。对比：`properties_t` 的默认值则由 `load_default_properties()` 在运行时显式填充（见 4.3），两种手法各有取舍。

**练习 3**：`config_save` 里先擦后写同一页，如果写到一半掉电，下次开机会发生什么？

答案：该页内容部分是新数据、部分是 0xFF（或旧值残留），magic 或 checksum 大概率校验失败，`config_recall` 返回 -1，固件退回编译期默认配置。用户损失的是个性化设置而非功能——这是「校验 + 默认值回退」换来的掉电安全性，代价是没有原子性（对比日志型文件系统会先写副本再切换）。

### 4.3 caldata_save / caldata_recall：校准槽的存取与全区擦除

#### 4.3.1 概念说明

`properties_t` 是「**一个完整测量现场**」的快照：频率边界、101 点频点表、5 组校准数据、电延迟、4 条轨迹配置、4 个 marker、速度因子、带宽档……共 4608 字节（0x1200）。 NanoVNA 提供 **5 个槽位**把它整个存进 flash，对应屏幕上的存储 0~4。

它与 `config_t` 的两点关键差异：

1. **体积**：0x1200 字节 > 一页 2KB，必须连擦 3 页，于是有了槽位间距问题；
2. **双指针**：读回时 `active_props` 指向 flash 槽（`cal_data` 别名读的是 flash 里的校准原文），`current_props` 是可改的 SRAM 工作副本——u3-l3 的插值、`ensure_edit_config` 都建立在这对指针上。

#### 4.3.2 核心流程

先看地址地图（宏定义在 [nanovna.h:L351-L361](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L351-L361)，与链接脚本 [STM32F072xB.ld:L20-L38](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/STM32F072xB.ld#L20-L38) 的 flash0=96K/flash7=32K 划分严格咬合）：

| 地址范围 | 内容 | 尺寸 |
|---|---|---|
| 0x08000000 ~ 0x08017FFF | 固件程序区（flash0） | 96K |
| **0x08018000** | `config_t` 全局配置 | 1 页（0x800） |
| **0x08018800** | 校准槽 0 | 3 页（0x1800） |
| **0x0801A000** | 校准槽 1 | 3 页 |
| **0x0801B800** | 校准槽 2 | 3 页 |
| **0x0801D000** | 校准槽 3 | 3 页 |
| **0x0801E800** | 校准槽 4（终于 0x0801FFFF） | 3 页 |

间距为什么是 0x1800（6144）而不是 sizeof 的 0x1200（4608）？因为**擦除粒度是 2KB 页，而 0x1200 不是页的整数倍**（=2.25 页）。若把 5 个槽紧紧相邻排布，第 1 槽的尾部会伸进第 2 槽占用的页——擦第 2 槽就会连带毁掉第 1 槽的数据。让每槽独占 3 页，各槽的擦除互不牵连；代价是每槽浪费 0x1800−0x1200 = 0x600（1536）字节。整个 32K 保存区恰好容纳 1 + 5×3 = 16 页，分毫不差地用完 STM32F072xB 的 128K flash。

保存流程（sweep 线程中执行）：

```
caldata_save(id):
    id 越界检查（0..4）
    current_props.magic    = CONFIG_MAGIC
    current_props.checksum = checksum(其余全部字段)
    flash_unlock()
    从槽首地址起，按 0x800 步进连擦 3 页           # 覆盖 0x1200 字节需 3 页
    把 current_props 逐半字写入槽（0x1200/2 = 2304 次）
    active_props = flash 槽地址                     # 校准数据权威版指向 flash
    lastsaveid    = id                              # 屏幕 C 标记显示槽号
```

恢复流程（开机或 RECALL 菜单）：

```
caldata_recall(id):
    id 越界 → load_default_properties() 并返回 -1
    src = flash 槽地址
    magic   不符 → load_default_properties() 并返回 -1
    checksum 不符 → load_default_properties() 并返回 -1
    active_props = src          # 指向 flash：cal_data 读的是存档原文
    lastsaveid   = id
    memcpy(&current_props, src, 0x1200)   # SRAM 工作副本
    return 0
```

注意与 `config_recall` 的对比：config 失败时**静默**退回编译期默认值（返回值没人检查），caldata 失败时显式调用 `load_default_properties()` 并由调用方打印 `Err, default load` 提示。

#### 4.3.3 源码精读

[nanovna.h:L363-L387](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L363-L387) —— `properties_t` 全貌与那句著名注释 `//sizeof(properties_t) == 0x1200`。两大数组占了绝对体积：`_frequencies[101]`（404 字节）与 `_cal_data[5][101][2]`（4040 字节），合计约 88%。头部五字段（magic/frequency0/frequency1/sweep_points/cal_status）对应 u3-l1 的三层频率表示，`_cal_status` 就是 u3-l3 讲的 CALSTAT_* 状态位。

[flash.c:L123-L130](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L123-L130) —— `saveareas[]` 地址表：五个槽首地址硬编码为常量表，`lastsaveid` 记录最近存取的槽号。

[flash.c:L132-L168](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L132-L168) —— `caldata_save` 主体。第 150-155 行的擦除循环值得逐字读：

```c
void *p = dst;
void *tail = p + sizeof(properties_t);
while (p < tail) {
    flash_erase_page((uint32_t)p);
    p += FLASH_PAGESIZE;
}
```

从槽首地址每次步进 0x800，直到越过 `槽首 + 0x1200`——共迭代 3 次（偏移 0、0x800、0x1000），擦净 3 页。（顺带一提：对 `void*` 做指针算术是 GCC 扩展，它按「元素大小为 1」处理，移植到严格编译器需改成 `char*`。）第 163-165 行是 u3-l3 的伏笔：存完把 `active_props` 拨向 flash 槽、记下 `lastsaveid`——此后 `#define cal_data active_props->_cal_data`（[nanovna.h:L400](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L400)）读到的就是刚落盘的这份。

[flash.c:L170-L197](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L170-L197) —— `caldata_recall`：三个 `goto load_default` 出口（越界/magic/checksum）+ 成功路径的「双写」：`active_props = src` 与 `memcpy(&current_props, src, ...)`。第 191 行注释解释了为什么要复制：「duplicate 到 SRAM 才能改 marker/trace」。

[flash.c:L199-L212](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L199-L212) —— `caldata_ref`：u3-l3 `cal_interpolate` 使用的只读入口——同样两级校验，但不做 memcpy、不改任何全局状态，失败返回 NULL。它是「借别的槽做插值源」的安全读法。

[flash.c:L214-L228](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/flash.c#L214-L228) —— `clear_all_config_prop_data`：从 `save_config_area`（0x08018000）一口气擦到 `+0x8000`，即 16 页 = config + 全部 5 槽，恢复出厂。它不做任何校验也不写回，配合 `clearconfig 1234` 的保护键与「手动复位生效」的提示（[main.c:L577-L579](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L577-L579)），因为擦除后 RAM 里仍是旧数据，须复位重新走默认值路径。

[main.c:L2402-L2405](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2402-L2405) —— 开机恢复序列：`config_recall()` 之后紧接着 `caldata_recall(0)`——**槽 0 是默认校准槽**，新机器首次开机时它校验失败，静默走默认值，用户无感知。

[main.c:L817-L840](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L817-L840) —— `load_default_properties`：失败回退的目标。注意注释明确说明 magic 与 checksum「由 caldata_save 补上」、`_frequencies` 与 `_cal_data`「默认不加载」（留给 update_frequencies 与校准流程填充）。

[main.c:L1518-L1550](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1518-L1550) —— shell 侧入口 `save {id}` / `recall {id}`：都做 0..4 越界检查，recall 失败时打印 `Err, default load` 并照常 `update_frequencies()`。

[ui.c:L478-L487](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L478-L487)、[ui.c:L509-L517](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L509-L517)、[ui.c:L527-L536](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L527-L536) —— 触屏菜单侧的三个回调：RECALL 槽 N、CONFIG→SAVE（存 config_t）、SAVE 槽 N。与 shell 命令殊途同归，验证了「同一套 flash API 被两种交互外壳复用」。

[plot.c:L1655-L1665](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1655-L1665) —— `draw_cal_status`：屏幕上校准标记的第三字符来自 `active_props == &current_props ? '*' : '0' + lastsaveid`——正在 RAM 中编辑显示 `*`，已落盘显示槽号数字。本讲写入的 `lastsaveid` 在这里变成用户可见的反馈。

#### 4.3.4 代码实践

**实践名称：用 Python 精确复算 properties_t 的布局与地址地图**（纯 PC 实践，本讲核心实践）

1. **实践目标**：验证 `sizeof(properties_t) == 0x1200`，弄清 `_reserved[49]` 的作用，并证明 5 个槽正好铺满 32K 保存区。
2. **操作步骤**：新建 `layout_check.py`：

   ```python
   # 示例代码：properties_t 布局计算器（对齐规则与 ARM 32 位 ABI 一致）
   import ctypes

   class TraceT(ctypes.Structure):
       _fields_ = [("enabled", ctypes.c_uint8), ("type", ctypes.c_uint8),
                   ("channel", ctypes.c_uint8), ("reserved", ctypes.c_uint8),
                   ("scale", ctypes.c_float), ("refpos", ctypes.c_float)]

   class MarkerT(ctypes.Structure):
       _fields_ = [("enabled", ctypes.c_int8), ("index", ctypes.c_int16),
                   ("frequency", ctypes.c_uint32)]

   POINTS = 101

   class PropertiesT(ctypes.Structure):
       _fields_ = [
           ("magic", ctypes.c_uint32),
           ("_frequency0", ctypes.c_uint32),
           ("_frequency1", ctypes.c_uint32),
           ("_sweep_points", ctypes.c_uint16),
           ("_cal_status", ctypes.c_uint16),
           ("_frequencies", ctypes.c_uint32 * POINTS),
           ("_cal_data", ctypes.c_float * (5 * POINTS * 2)),  # [5][101][2] 行优先展平
           ("_electrical_delay", ctypes.c_float),
           ("_trace", TraceT * 4),
           ("_markers", MarkerT * 4),
           ("_velocity_factor", ctypes.c_float),
           ("_active_marker", ctypes.c_int8),
           ("_domain_mode", ctypes.c_uint8),
           ("_marker_smith_format", ctypes.c_uint8),
           ("_bandwidth", ctypes.c_uint8),
           ("_freq_mode", ctypes.c_int8),
           ("_reserved", ctypes.c_uint8 * 49),
           ("checksum", ctypes.c_uint32),
       ]

   class ConfigT(ctypes.Structure):
       _fields_ = [("magic", ctypes.c_int32),
                   ("dac_value", ctypes.c_uint16),
                   ("grid_color", ctypes.c_uint16),
                   ("menu_normal_color", ctypes.c_uint16),
                   ("menu_active_color", ctypes.c_uint16),
                   ("trace_color", ctypes.c_uint16 * 4),
                   ("touch_cal", ctypes.c_int16 * 4),
                   ("harmonic_freq_threshold", ctypes.c_uint32),
                   ("vbat_offset", ctypes.c_uint16),
                   ("_reserved", ctypes.c_uint8 * 22),
                   ("checksum", ctypes.c_uint32)]

   print("sizeof(properties_t) =", hex(ctypes.sizeof(PropertiesT)))
   print("checksum 偏移        =", hex(PropertiesT.checksum.offset))
   print("sizeof(config_t)     =", hex(ctypes.sizeof(ConfigT)))

   # 地址地图：对照 nanovna.h 的 SAVE_* 宏
   SLOT_STRIDE, PAGE, BASE = 0x1800, 0x800, 0x08018000
   print(f"config   @ {BASE:#010x}，占 1 页")
   for i in range(5):
       a = 0x08018800 + i * SLOT_STRIDE
       print(f"slot {i}  @ {a:#010x}，擦 {ctypes.sizeof(PropertiesT) // PAGE + 1} 页，"
             f"槽区终于 {a + SLOT_STRIDE:#010x}")
   ```

   先手工填一张偏移表作为「标准答案」，再跑脚本对照：

   | 字段 | 偏移 | 大小 |
   |---|---|---|
   | magic / _frequency0 / _frequency1 | 0x0000 / 0x0004 / 0x0008 | 各 4 |
   | _sweep_points / _cal_status | 0x000C / 0x000E | 各 2 |
   | _frequencies[101] | 0x0010 | 404 (0x194) |
   | _cal_data[5][101][2] | 0x01A4 | 4040 (0xFC8) |
   | _electrical_delay | 0x116C | 4 |
   | _trace[4]（每条 12 字节） | 0x1170 | 48 |
   | _markers[4]（每个 8 字节，含 1 字节填充） | 0x11A0 | 32 |
   | _velocity_factor | 0x11C0 | 4 |
   | _active_marker … _freq_mode（5 个字节字段） | 0x11C4~0x11C8 | 各 1 |
   | _reserved[49] | 0x11C9 | 49 |
   | （尾部对齐填充） | 0x11FA | 2 |
   | checksum | 0x11FC | 4 |
   | **总计** | | **0x1200** |

3. **需要观察的现象**：`sizeof` 是否等于 0x1200；`checksum.offset` 是否为 0x11FC；槽 4 的槽区终点是否恰好是 0x08020000（128K flash 的尽头）。
4. **预期结果**：`sizeof(properties_t) = 0x1200`，与 [nanovna.h:L387](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L387) 的注释一致；`checksum` 偏移 0x11FC；5 个槽 + 前面 1 页 config 正好用完 16 页 32K。
5. **思考 `_reserved[49]` 的双重作用**（对照上表回答）：
   - **前向兼容**：去掉它结构体只有约 0x11D0 字节。留出 49 字节空位，将来新增要持久化的字段（如 u5-l4 毕业项目想存的 average 次数）可以吃掉 `_reserved` 的空间，**结构体总长和 checksum 偏移保持 0x1200 不变**——旧固件存的槽、新固件照读不误（新字段读到 0 也只是零值）。
   - **取整对齐**：49 字节保留区 + 2 字节尾部填充，把总长从 4560 圆整到 4608（0x1200），让「sizeof 是 256 的倍数」这一事实直接写在注释里，方便链接脚本与槽距设计估算。
6. **链接脚本对照**：阅读 [STM32F072xB.ld:L66-L96](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/STM32F072xB.ld#L66-L96)：`CALDATA_FLASH` 别名指向 flash7，`.calsave` 段标记 `NOLOAD`——烧录工具写固件镜像时不会生成这一段的下载记录，所以 DFU 升级固件**不会擦掉**保存区，校准数据得以幸存（承接 u1-l2 的结论，现在你能指出它成立的两个条件：地址划分在链接脚本里、段属性是 NOLOAD）。

#### 4.3.5 小练习与答案

**练习 1**：`caldata_save` 擦了 3 页却只写 0x1200 字节，第 3 页的尾部（槽内偏移 0x1200~0x17FF）是什么状态？这部分空间被谁「占用」？

答案：保持擦除态全 0xFF，约 1536 字节被浪费。它被「租」给页擦除粒度：因为 0x1200 不是 0x800 的整数倍且槽不能共享页（否则擦 A 槽毁 B 槽尾部），每个槽必须独占 3 页。这是用空间换实现简单与槽间隔离。

**练习 2**：`caldata_recall` 成功路径为什么「既把 active_props 指向 flash，又 memcpy 到 current_props」两件事都做？

答案：两个指针服务两类读者——`active_props` 让 `cal_data` 别名（[nanovna.h:L395-L410](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L395-L410)）读到 flash 里的**校准数据权威原文**（apply_error_term、cal_interpolate 都从这里取），而 `current_props` 是**可写工作副本**，marker、trace、频率的日常修改都发生在 SRAM。u3-l3 的 `ensure_edit_config` 负责在用户动手改校准时把 `active_props` 切回 `&current_props` 并清 CALSTAT_APPLY，`draw_cal_status` 则靠 `active_props == &current_props` 判断显示 `*` 还是槽号。

**练习 3**：如果把 POINTS_COUNT 从 101 改成 201（假设 sweep 支持更多点），`properties_t` 与地址布局会发生什么连锁变化？

答案：`_frequencies` 变 804 字节、`_cal_data` 变 5×201×2×4 = 8040 字节，结构体总长超过 0x1200 但仍小于 0x1800，槽距尚可维持；但 [nanovna.h:L387](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L387) 的注释、checksum 偏移都会变，且 `_reserved` 的兼容缓冲被压缩。若继续增大到超过 0x1800，就必须重新设计槽距（比如 4 页间距）并同步修改 SAVE_PROP_CONFIG_* 五个宏——这套「地址宏硬编码」的设计把布局知识固化在两个文件里（nanovna.h 与 STM32F072xB.ld），改动必须两边一致，这也是它最脆弱的地方。

## 5. 综合实践

**综合实践：写一份 `flash_layout.py`「存档区体检报告」脚本，把本讲三个模块串起来。**

任务要求脚本输出一份完整报告，包含以下内容（前两项复用 4.3.4 的代码）：

1. **结构体布局表**：用 ctypes 打印 `properties_t` 每个字段的 `偏移/大小`（遍历 `_fields_`，`getattr(PropertiesT, name).offset`），核对总量 0x1200；同时给出 `config_t`（60 字节）的布局。
2. **地址地图**：打印 16 页保存区的完整分配（1 页 config + 5×3 页槽），标注每页归属；并检查两个不变式——槽距 0x1800 是 0x800 的整数倍（保证槽界与页界对齐）、槽 4 结束地址 == 0x08020000。
3. **写入代价模拟**：用 4.1.4 的 `flashsim.py` 思路扩展为 3 页连续空间，模拟一次完整的 `caldata_save(2)`：先对槽 2 的 3 页执行 `erase_page()`，再逐半字写入 0x1200 字节的假数据（magic 用真值 0x434F4E45，checksum 用 4.2.4 的 rotate 算法对前 0x11FC 字节计算）。然后模拟「写一半掉电」（只写一半就停），实现 `recall()` 函数按 magic/checksum 两级校验判断存档是否可用，验证半份存档会被正确判废并触发默认值回退。

验收标准（自检）：

- 报告里的偏移表与 4.3.4 的手工表逐行一致；
- 「写一半掉电」实验中 `recall()` 返回失败（checksum 不符），完整写入时返回成功；
- 能用一句话向同伴解释：为什么改一个 marker 位置也要重擦 3 页 flash、以及为什么 NanoVNA 仍接受这个代价。

有真机的读者可加一步闭环验证：`save 3` 存槽 3 → 断电重启 → 屏幕 CONFIG/校准标记应显示 `3`（即 `lastsaveid` 生效，见 [plot.c:L1665](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1665)）→ `recall 1` 切到空槽应看到 `Err, default load` 并回到默认扫描设置。

## 6. 本讲小结

- NanoVNA 不用文件系统，而是把 `config_t`（60 字节，1 页）与 5 份 `properties_t`（各 0x1200 字节，独占 3 页）按固定地址直接写进 32K 保存区；地址宏在 nanovna.h，与链接脚本的 flash0/flash7 划分严格咬合，`.calsave` 为 NOLOAD 使 DFU 升级不丢校准。
- flash 三定律决定了代码形态：擦除按 2KB 页、编程只能 1→0、最小单位半字——所以每个 save 都是「解锁 → 连擦 N 页 → 逐半字写回」。
- `magic + rotate checksum` 构成两级校验链：magic 粗筛「从未写过/格式不对」，checksum（ \( v_{i+1}=\mathrm{ROR}(v_i,31)+w_i \)，位置敏感）细查局部损坏；两级都在 flash 原地校验，通过才 memcpy 进 RAM。
- 失败回退有两条路：`config` 靠编译期初始化器（.data 段）静默兜底；`properties` 靠显式的 `load_default_properties()` 并向调用方返回 -1。
- `caldata_recall/save` 维护 `active_props` 双指针与 `lastsaveid`：前者让校准数据读 flash 原文、日常修改发生在 SRAM 副本（承接 u3-l3），后者把存档槽号变成屏幕上可见的 `C0`~`C4` 标记。
- `_reserved[49]` 一举两得：给未来新增持久化字段留出不动布局的空位，同时把结构体圆整到 0x1200。

## 7. 下一步学习建议

- **u3-l5（时域变换）**：`properties_t._domain_mode` 字段保存的正是时域模式配置，下一讲看 `transform_domain` 如何用 FFT 把 101 点频域数据变到距离域——其中会再次看到 `spi_buffer` 的复用与 0x1200 结构体之外的 RAM 压力。
- **阅读 `cal_interpolate`（main.c）**：本讲的 `caldata_ref` 是它的数据入口，结合 4.3 的槽位布局，理解「借槽插值」如何做到只读 flash、只写 RAM。
- **延伸阅读**：对照 STM32F0 参考手册的 FLASH 章节，确认 KEYR 魔数、CR 的 PG/PER/STRT 位定义与页擦除时序，把本讲的寄存器序列和官方文档逐条对上；若想体会「带磨损均衡的掉电存储」，可以对比 littlefs 或 EEPROM 仿真方案的思路，思考 NanoVNA 为什么可以不用它们（提示：保存频率极低、数据量固定、且有默认值兜底）。
