# 掉电不丢：配置的 Flash 持久化

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 STM32F303 Flash 控制器的三步写操作：解锁 KEYR → 页擦除（PER + AR + STRT）→ 半字编程（PG），以及每一步对应的寄存器时序。
- 解释 `flash_erase_page()` 为什么要用 `chSysLock()`/`chSysUnlock()` 包起来，而 `flash_program_half_word()` 却没有加锁。
- 读懂 `config_t` 的完整字段布局：100 个信道、`uistat` 整机状态、AGC 参数、硬件版本修正量是如何排进一块不到 1KB 的数据里的。
- 推导 XOR 校验和的数学原理：为什么保存前先把 `checksum` 字段清零、读取时对整块（含 checksum 自身）再异或一次结果必须为 0。
- 说明配置页为什么固定选在 `0x0801f800`（128KB Flash 的最后一页），以及它和链接脚本之间**只靠约定、不靠机制**保证的安全边界。

本讲是单元四的收尾：前四讲里屏幕上看到的一切（信道、频率、音量、AGC 档位、屏幕旋转方向……）之所以能在断电重启后原样回来，全靠本讲的这一小块代码。

## 2. 前置知识

### 2.1 RAM 与 Flash 的根本区别

RAM（本机是 40KB SRAM）读写快、任意字节可改，但**掉电即丢**。Flash（本机是 128KB NOR Flash）掉电不丢，但写入受物理规律限制：

- **编程（写）只能把位从 1 变成 0**，不能把 0 变回 1。
- 想把 0 变回 1，必须**擦除（erase）**，而擦除的最小单位不是字节，是**一整页**——本器件一页 2KB。
- 擦除后整页变成全 `0xFF`（全 1），之后才能重新编程。

所以"改一个字节"在 Flash 上实际是"擦掉 2KB → 把整块数据重新写回去"。这正是 `config_save()` 的做法。

顺带一提：STM32F303 片内**没有 EEPROM**。很多单片机用 EEPROM 存配置（可以按字节改写），F303 的方案只有一条路——拿主 Flash 的某一页当"伪 EEPROM"用。

### 2.2 XOR（异或）校验和

XOR 运算记作 \( \oplus \)，有一条关键性质——**自反律**：

\[ x \oplus x = 0 \qquad x \oplus 0 = x \]

把数据按 32 位字切块全部异或起来，得到一个"校验和"。数据中任何一个字节发生变化，都会让所在的那个字改变，从而改变异或结果。于是：

- 数据完好 → 重算结果等于预期值；
- 数据有任何**单处**损坏 → 结果必然偏离。

这就是最廉价的完整性检查。它不如 CRC 强壮（两处对称的损坏可能互相抵消），但对"Flash 页写了一半断电""读到上电随机态"这类故障已经足够，而且一条 XOR 指令就能算完。

### 2.3 链接脚本与 Flash 版图（承接 u1-l2）

回顾 u1-l2：链接器按 `STM32F303xB.ld` 里的 `MEMORY` 命令把固件各段（text/data/bss）排进地址空间。Flash 从 `0x08000000` 开始共 128KB。本讲要回答的问题是：**代码从低地址往高地址排，配置数据放在最高处的最后一页，两者怎么保证不打架？** 答案比你想的脆弱，见 4.4 节。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [flash.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c) | 全部 Flash 底层操作：解锁、页擦除、半字编程、XOR 校验和、`config_save`/`config_recall`/`clear_all_config_prop_data` |
| [nanosdr.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h) | `config_t`、`uistat_t`、`channel_t` 结构体定义、`CHANNEL_MAX`、`CONFIG_MAGIC` |
| [main.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c) | `config` 全局变量及其出厂默认值、`save`/`clearconfig`/`channel` shell 命令、`main()` 里的 `config_recall()` 调用、`save_config_current_channel()` |
| [ui.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c) | 长按旋钮触发保存、`recall_channel()` 从 `config.channels[]` 召回信道 |
| [STM32F303xB.ld](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/STM32F303xB.ld) | `MEMORY` 区域声明——分析配置页选址安全性的依据 |

## 4. 核心概念与源码讲解

### 4.1 Flash 控制器驱动：解锁、页擦除、半字编程

#### 4.1.1 概念说明

STM32 的 Flash 在复位后默认是**写保护**的——防止程序跑飞时误写自身。要写 Flash 必须先向 `KEYR` 寄存器按顺序写入两个魔数"钥匙"解锁。之后每次操作走同一套模式：

1. 等 `SR` 寄存器的 `BSY` 位清零（上一次操作结束）；
2. 在 `CR` 寄存器里选择操作类型（`PER` = 页擦除，`PG` = 编程）；
3. 页擦除要先把目标地址写进 `AR` 寄存器、再置 `STRT` 位启动；编程则直接向 Flash 地址做一次 16 位写访问，硬件拦截这次访问完成烧写；
4. 再等 `BSY` 清零，最后把 `CR` 里的操作位清掉复原。

注意半字（16 位）这个粒度：F3 的 Flash 控制器按 16 位为单位编程，这就是为什么后面 `config_save()` 用 `uint16_t*` 指针逐个半字搬运。

#### 4.1.2 核心流程

```
flash_unlock()
  └─ KEYR ← 0x45670123; KEYR ← 0xCDEF89AB   （官方规定的钥匙序列）

flash_erase_page(addr)          ← 包在 chSysLock/chSysUnlock 之间
  └─ flash_erase_page0(addr)
       ├─ 等 BSY 清零
       ├─ CR |= PER              （选择"页擦除"）
       ├─ AR  = addr             （擦哪一页）
       ├─ CR |= STRT             （启动，硬件开始忙）
       ├─ 等 BSY 清零            （擦一页耗时毫秒级，见数据手册）
       └─ CR &= ~PER             （复原）

flash_program_half_word(addr, data)
  ├─ 等 BSY 清零
  ├─ CR |= PG                    （选择"编程"）
  ├─ *(volatile uint16_t*)addr = data   （向 Flash 地址写入即触发烧写）
  ├─ 等 BSY 清零
  └─ CR &= ~PG
```

#### 4.1.3 源码精读

先看忙等与擦除。[flash.c:L25-L31](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c#L25-L31) 是所有操作共用的等待函数，自旋等 `FLASH->SR` 回到非忙状态：

```c
static int flash_wait_for_last_operation(void)
{
  while (FLASH->SR == FLASH_SR_BSY) {
    //WWDG->CR = WWDG_CR_T;
  }
  return FLASH->SR;
}
```

注意这里用的是 `==`（整寄存器相等）而不是按位与 `& FLASH_SR_BSY`——正常路径下两者等价（忙时 SR 恰好只有 BSY 置位），但如果错误标志（如 WRPERR）在忙期间置位，`==` 会提前退出循环。这是一个值得留意的写法，见 4.1.5 练习 3。

[flash.c:L33-L49](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c#L33-L49) 是页擦除。注意公开接口 `flash_erase_page()` 用 `chSysLock()`/`chSysUnlock()` 把裸操作包起来：

```c
static void flash_erase_page0(uint32_t page_address)
{
	flash_wait_for_last_operation();
	FLASH->CR |= FLASH_CR_PER;
	FLASH->AR = page_address;
	FLASH->CR |= FLASH_CR_STRT;
	flash_wait_for_last_operation();
	FLASH->CR &= ~FLASH_CR_PER;
}

int flash_erase_page(uint32_t page_address)
{
  chSysLock();
  flash_erase_page0(page_address);
  chSysUnlock();
  return 0;
}
```

`chSysLock()` 是 ChibiOS 的内核锁：把当前线程钉在 CPU 上、阻止内核级抢占切换。擦一页要忙等毫秒级时间，如果这期间线程被切走、"设置 PER → 写 AR → 置 STRT"的序列被另一个也想动 Flash 的线程插队，寄存器状态就乱了。锁的含义是：**擦除序列必须原子完成**。至于它对实时性的代价，见 4.4.4 的讨论。

[flash.c:L51-L58](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c#L51-L58) 是半字编程。关键一行是 `*(__IO uint16_t*)address = data;`——这不是普通内存写，而是"向 Flash 地址空间写半字"这一动作本身被 Flash 控制器解释为烧写命令：

```c
void flash_program_half_word(uint32_t address, uint16_t data)
{
	flash_wait_for_last_operation();
	FLASH->CR |= FLASH_CR_PG;
    *(__IO uint16_t*)address = data;
	flash_wait_for_last_operation();
	FLASH->CR &= ~FLASH_CR_PG;
}
```

它没有加内核锁（调用它的 `config_save()` 也没有）——本固件里保存配置只发生在 shell 线程和 UI 长按两处，且每个半字编程只忙等几微秒，作者显然只对"长操作加锁、短操作靠调用约定"做了取舍。

[flash.c:L60-L65](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c#L60-L65) 是解锁：向 `KEYR` 按序写入两把官方钥匙 `0x45670123`、`0xCDEF89AB`（任何 STM32 参考手册都会给出这两个值）。解锁后 Flash 保持可写直到下次复位。

#### 4.1.4 代码实践

1. **实践目标**：不用任何硬件，把"页地址"这笔账算明白，并验证 `0x0801f800` 确实是 128KB 器件的最后一页。
2. **操作步骤**：
   - 读 [flash.c:L80-L84](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c#L80-L84)：`FLASH_PAGESIZE` 为 `0x800`（2048 字节），`save_config_area` 为 `0x0801f800`。
   - 手算：128KB = 0x20000，页数 = 0x20000 / 0x800 = 64 页。最后一页起始地址 = 0x08000000 + (64−1)×0x800 = 0x08000000 + 0x1F800 = **0x0801F800**。
   - 再验证 `clear_all_config_prop_data()`（[flash.c:L127-L139](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c#L127-L139)）：`save_config_prop_area_size` 也是 0x800，所以它的 while 循环只跑一轮，恰好擦这一页。
3. **需要观察的现象**：纯纸面推导，无运行现象。
4. **预期结果**：算出的地址与代码常量逐位一致；同时理解注释"assume STM32F303CBT6 flash 128k Device"的含义——这个常量是**按具体器件写死的**，换 256KB 的 F303 就不是最后一页了。

#### 4.1.5 小练习与答案

**练习 1**：为什么擦除之后 Flash 读出来是 `0xFF`，而不是 `0x00`？

答案：NOR Flash 的物理约定是"编程把 1 写成 0"（对应浮栅充电），擦除把整页恢复成全 1（全 `0xFF`）。因为只有 1→0 一个方向可写，所以想把任何 0 改回 1 都必须整页擦除重来。

**练习 2**：`flash_program_half_word` 的写入粒度是 16 位。如果 `config_t` 的大小是奇数字节，`config_save()` 的半字循环会发生什么？

答案：`count = sizeof(config_t)/2` 会向下取整，**最后一个孤立字节被丢弃**，该字节在 Flash 里保持擦除态 0xFF。本工程中 `config_t` 以 `int32_t checksum` 结尾、总大小是 4 的倍数（见 4.2.3 的推算），不会踩到这个坑——但给结构体加字段时必须记住这条隐性约束。

**练习 3**：`while (FLASH->SR == FLASH_SR_BSY)` 与 `while (FLASH->SR & FLASH_SR_BSY)` 有什么区别？哪种更稳健？

答案：`==` 要求 SR **整个寄存器的值恰好等于** BSY 位掩码；`&` 只关心 BSY 位是否置位。若忙期间错误标志（WRPERR 等）置位，SR 变成 BSY|错误位，`==` 判断为假会**提前退出等待**，而 `&` 会继续等到 BSY 真正清零。按位与 `&` 是更稳健的惯用法；`==` 在"无错误发生的正常路径"下行为相同。这属于源码可改进点，读懂即可。

### 4.2 存什么：config_t 的字段与存储布局

#### 4.2.1 概念说明

"配置"不只是音量大小。`config_t` 是这台机器**完整记忆**的快照，分五类：

1. **校验头尾**：`magic`（开头，'CONF' 标记）+ `checksum`（结尾，XOR 值）——给整块数据上"封条"。
2. **模拟修正量**：`dac_value`（DAC 初始值）、`agc`（7 个整数的 AGC 参数组，对应 u2-l2 讲过的 `tlv320aic3204_agc_config_t`）。
3. **100 个信道库**：`channels[CHANNEL_MAX]`，每条只有频率和调制模式两个有效字段——这是"电台预存台位"功能。
4. **整机状态**：`uistat`——u4-l4 讲的 UI 状态机现场（当前档位、频率、音量、rfgain、显示模式、CW 侧音、IQ 平衡……）整个结构体原样快照。
5. **硬件版本修正量**：`freq_inverse`、`button_polarity`（区分 revision 0/1 板子的按键极性）、`lcd_rotation`（u2-l4 讲的 180 度旋转持久化）。

#### 4.2.2 核心流程

写入方（保存）和读出方（召回）共用同一份 RAM 镜像 `config`：

```
运行时                      Flash 最后一页 (0x0801f800)
──────                      ─────────────────────────
uistat（活的状态机现场）
   │ 保存时快照
   ▼
config（RAM 镜像，出厂默认值见 main.c:120）
   │ config_save(): 算校验和→擦页→444 个半字逐个编程
   ▼
                          config_t 的 888 字节镜像（推算值）
   ▲
   │ config_recall(): magic 对？整块 XOR==0？→ memcpy 回 config
uistat = config.uistat（main.c:968，开机一次性恢复）
```

#### 4.2.3 源码精读

[main.c:L819-L828](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L819-L828) 印证了"保存的就是 `config` 这个全局变量"：

```c
static void cmd_save(BaseSequentialStream *chp, int argc, char *argv[])
{
  ...
  config.uistat = uistat;   // 先把活的 UI 现场快照进 config
  config_save();            // 再把整个 config 落 Flash
  chprintf(chp, "Config saved.\r\n");
}
```

[main.c:L247-L256](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L247-L256) 是长按保存的落点：先把**当前信道**的频率/模式写回信道库，再快照 `uistat`、整块保存：

```c
void
save_config_current_channel(void)
{
  int channel = uistat.channel;
  config.channels[channel].freq = uistat.freq;
  config.channels[channel].modulation = uistat.modulation;

  config.uistat = uistat;
  config_save();
}
```

结构体定义在 [nanosdr.h:L284-L299](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L284-L299)：

```c
#define CHANNEL_MAX 100

typedef struct {
  uint32_t freq;
  modulation_t modulation;
} channel_t;

typedef struct {
  int32_t magic;
  uint16_t dac_value;
  tlv320aic3204_agc_config_t agc;
  channel_t channels[CHANNEL_MAX];
  uistat_t uistat;
  int8_t freq_inverse;
  uint8_t button_polarity;
  int8_t lcd_rotation;
  int32_t checksum;
} config_t;
```

`uistat_t` 本体在 [nanosdr.h:L256-L274](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L256-L274)（u4-l4 已逐字段讲过），`CONFIG_MAGIC` 在 [nanosdr.h:L303](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L303)：`0x434f4e45`，四个字节正是 ASCII 的 `'C','O','N','F'`——用十六进制转储 Flash 时一眼就能认出"这是一份有效配置"。

按 ARM EABI 对齐规则（枚举占 4 字节、按成员自然对齐补 padding），可推算出布局（**具体数值待本地验证**，验证方法见 4.2.4）：

| 字段 | 偏移 | 大小 | 说明 |
|---|---|---|---|
| `magic` | 0 | 4 | 'CONF' 标记 |
| `dac_value` | 4 | 2 | DAC 初值 |
| （padding） | 6 | 2 | 对齐到 4 |
| `agc` | 8 | 28 | 7 × int |
| `channels[100]` | 36 | 800 | 每条 8 字节 × 100 |
| `uistat` | 836 | 44 | 见 u4-l4 |
| `freq_inverse` | 880 | 1 | |
| `button_polarity` | 881 | 1 | |
| `lcd_rotation` | 882 | 1 | |
| （padding） | 883 | 1 | 给 checksum 对齐 |
| `checksum` | 884 | 4 | XOR 封条 |
| **合计** | | **888** | 444 个半字，一页 2048 字节绰绰有余 |

出厂默认值在 [main.c:L120-L163](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L120-L163)：开机频率 567kHz、AM 模式、AGC_MID、rfgain 40，外加 18 条预置广播/业余频段信道。**这份静态初始化的全局变量就是"Flash 里没有有效配置时"的唯一兜底**——回忆 u1-l3：`config_recall()` 失败时固件静默回退到的正是它。

还有一个容易误会的细节：shell 的 `channel save` 命令（[main.c:L763-L775](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L763-L775)）只更新 **RAM 镜像**里的 `config.channels[]`，**并不立即写 Flash**：

```c
config.channels[channel].freq = uistat.freq;
config.channels[channel].modulation = uistat.modulation;
```

真正落 Flash 要等下一次 `save` 命令或长按旋钮。这样把"改内存"和"擦写 Flash"解耦，减少擦写次数（Flash 每页寿命约 1 万次量级）。

#### 4.2.4 代码实践

1. **实践目标**：在 PC 上实测 `config_t` 的大小与各字段偏移，验证 4.2.3 的推算表。
2. **操作步骤**：把 `nanosdr.h` 里 `tlv320aic3204_agc_config_t`、`uistat_t`、`channel_t`、`config_t` 的定义（连同各匿名枚举）拷进一个新的 `layout.c`（**示例代码**，不进工程），头部加 `#include <stdio.h>`、`#include <stdint.h>`、`#include <stddef.h>`，枚举字段可用 `int` 替代（ARM EABI 下枚举即 int）：

   ```c
   /* layout.c —— 示例代码：PC 端验证 config_t 布局 */
   printf("sizeof(config_t) = %zu\n", sizeof(config_t));
   printf("sizeof(uistat_t) = %zu\n", sizeof(uistat_t));
   printf("offsetof(config_t, channels) = %zu\n", offsetof(config_t, channels));
   printf("offsetof(config_t, uistat)   = %zu\n", offsetof(config_t, uistat));
   printf("offsetof(config_t, checksum) = %zu\n", offsetof(config_t, checksum));
   ```

   用本机 `gcc -o layout layout.c && ./layout` 运行（x86-64 与 ARM EABI 的结构体对齐规则在这些都是 4 字节标量成员时一致）。
3. **需要观察的现象**：打印出的各偏移与总大小。
4. **预期结果**：`sizeof(config_t) = 888`、`channels` 偏移 36、`uistat` 偏移 836、`checksum` 偏移 884；且 888 % 4 == 0（满足 4.3 节校验和按 32 位字扫描的前提）、888 / 2 = 444（半字循环次数）。若与你手推的不一致，差异多半出在枚举大小或 padding 上。**待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：`channels[100]` 占了整个结构体的绝大部分（800/888）。如果只想把信道条数减到 50 以省 Flash，要改哪几处？

答案：改 [nanosdr.h:L282](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L282) 的 `CHANNEL_MAX` 即可，`config_t` 自动缩小。但要小心副作用：`cmd_channel`、`ui_process` 里的 `minmax(uistat.channel + tick, 0, CHANNEL_MAX)` 等都用它做边界；且**旧 Flash 里保存的 888 字节镜像与新布局不兼容**——好在本工程没有版本迁移代码，改了结构体就该用 `clearconfig` 清一次 Flash。

**练习 2**：`magic` 为什么选 `'CONF'`（0x434f4e45）这种"可读"的值，而不是随便一个数（比如 0x12345678）？

答案：功能上任何数都行。选 ASCII 可读值是为了**可调试性**：用 `data` 命令或调试器转储配置页时，第一眼看到 `45 4e 4f 43`（小端）就知道"这页是一份有效配置"；新芯片/刚擦除的页则是 `ff ff ff ff`，一眼区分。这是嵌入式固件的常见习惯。

**练习 3**：`channel_t` 里存了 `modulation` 却没存 `rfgain`、`volume`——从产品角度看这是刻意的吗？

答案：是取舍。每信道只记"这是个什么台"（频率+模式），而音量/增益属于**会话级**设置放 `uistat`。uic 里 `recall_channel()` 被注释掉的那行 `//uistat.rfgain = config.channels[channel].rfgain`（[ui.c:L213](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L213)）说明作者试过把增益放进信道、后来放弃了——换台时不希望音量/增益跳变。

### 4.3 怎么存取：config_save / config_recall 与 XOR 校验和

#### 4.3.1 概念说明

`config_save()` 与 `config_recall()` 是一对镜像函数：一个把 RAM 里的 `config` 烧进 Flash，一个把 Flash 的内容搬回 RAM。安全性靠**两道关卡**：

- **关卡一（magic）**：Flash 页必须以 'CONF' 开头。新焊的芯片、刚擦除的页、被别的程序写过的页都过不了这关——快速排除"这不是我的数据"。
- **关卡二（checksum）**：把整块数据（含 checksum 字段自身）按 32 位字全部 XOR，结果必须恰好为 0。它拦住"开头像配置、但内容损坏"的页——比如保存过程中断电写了一半。

为什么"含 checksum 自身异或结果为 0"能成立？设结构体按 32 位字切成 \( w_0, w_1, \dots, w_{n-1} \)，其中最后一个字 \( w_{n-1} \) 就是 checksum 字段。保存时先令 \( w_{n-1} = 0 \)，算出

\[ s = \mathrm{len} \oplus w_0 \oplus w_1 \oplus \cdots \oplus w_{n-2} \oplus 0 \]

把 \( s \) 写进 checksum 字段再烧录。读取时对**整块**（这次 checksum 字段里已经是 \( s \)）重算：

\[ s' = \mathrm{len} \oplus w_0 \oplus \cdots \oplus w_{n-2} \oplus s = s \oplus s = 0 \]

最后一步用了自反律 \( x \oplus x = 0 \)。于是"校验通过"的判据就是异或值**等于零**本身，连期望值都不用另存。代价是：任一数据字 \( w_i \) 损坏（\( w_i \to w_i' \)）会让 \( s' = w_i \oplus w_i' \neq 0 \) 被检出，但**两处恰好相互抵消的损坏**会漏检——这是 XOR 校验（本质是逐位奇偶校验）的固有极限，CRC 才能更强。

#### 4.3.2 核心流程

```
config_save()
  ├─ 填 magic = 'CONF'
  ├─ checksum 字段清零 → 算整块 XOR → 写回 checksum 字段
  ├─ flash_unlock()                      （两把钥匙）
  ├─ flash_erase_page(0x0801f800)        （整页变 0xFF，带内核锁）
  └─ for 半字 in 0..443:                 （888 字节 = 444 个半字）
       flash_program_half_word(dst, *src++)

config_recall()
  ├─ Flash 页首 4 字节 == 'CONF' ？   否 → return -1
  ├─ 整块（含 checksum）XOR == 0 ？   否 → return -1
  └─ memcpy(&config, flash页, 888)    → return 0
```

#### 4.3.3 源码精读

校验和函数在 [flash.c:L68-L77](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c#L68-L77)：

```c
static uint32_t
checksum(const void *start, size_t len)
{
  uint32_t *p = (uint32_t*)start;
  uint32_t *tail = (uint32_t*)(start + len);
  uint32_t value = len;
  while (p < tail)
    value ^= *p++;
  return value;
}
```

三个细节：初值是 `len` 而不是 0（顺手把"长度"也掺进校验，改结构体后旧镜像更难侥幸通过）；按 `uint32_t` 步进，所以 `sizeof(config_t)` 必须是 4 的倍数（888 满足）；`start + len` 是对 `void*` 做加法——GNU 扩展，按字节步进，GCC 下合法。

[flash.c:L86-L109](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c#L86-L109) 是保存主流程，注意那三行校验和的"清零→计算→回填"次序：

```c
int
config_save(void)
{
  uint16_t *src = (uint16_t*)&config;
  uint16_t *dst = (uint16_t*)save_config_area;
  int count = sizeof(config_t) / sizeof(uint16_t);

  config.magic = CONFIG_MAGIC;
  config.checksum = 0;                       // 先清零
  config.checksum = checksum(&config, sizeof config);  // 再在"含零"状态下计算并回填

  flash_unlock();
  flash_erase_page((uint32_t)dst);           // 整页擦除
  while(count-- > 0) {                       // 444 个半字逐个编程
    flash_program_half_word((uint32_t)dst, *src++);
    dst++;
  }
  return 0;
}
```

[flash.c:L111-L125](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c#L111-L125) 是召回，两道关卡次序固定、任一失败立即返回 −1：

```c
int
config_recall(void)
{
  const config_t *src = (const config_t*)save_config_area;
  void *dst = &config;

  if (src->magic != CONFIG_MAGIC)
    return -1;
  if (checksum(src, sizeof(config_t)) != 0)
    return -1;

  /* duplicated saved data onto sram to be able to modify marker/trace */
  memcpy(dst, src, sizeof(config_t));
  return 0;
}
```

注意源码注释点明了为什么必须 memcpy 回 RAM 而不能直接把 `config` 指到 Flash：Flash 里的数据**只能整页重写、不能就地修改**，而 `config.channels[]`、`config.uistat` 在运行期是频繁读写的活数据。所以架构必然是"Flash 镜像 ↔ RAM 工作副本"两份。

调用侧在 [main.c:L955-L968](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L955-L968)，`config_recall()` 的返回值**被忽略**——失败时什么都不做，全局 `config` 保持 main.c:120 的静态默认值，这就是 u1-l3 说的"静默回退"：

```c
  halInit();
  chSysInit();

  /* restore config */
  config_recall();            // 返回值被忽略；失败 = 保持出厂默认

  if (config.button_polarity != 0) { ... }   // 立刻用到了 config 里的字段

  // copy uistat from uistat
  uistat = config.uistat;    // 整机 UI 状态现场恢复
```

#### 4.3.4 代码实践

1. **实践目标**：在 PC 上复刻 `config_save`/`config_recall` 的完整逻辑，然后**故意改坏一个字节**，亲眼验证第二道关卡拦截（`config_recall` 返回 −1、RAM 副本保持默认值不动）。
2. **操作步骤**：
   - 新建 `sim_config.c`（**示例代码**，不进固件工程）。结构体定义从 4.2.4 的 `layout.c` 复用；再用一个 2KB 数组模拟 Flash 页，擦除即 `memset(0xFF)`，半字编程即按小端写两字节；`checksum`/`config_save`/`config_recall` 三段逻辑从 flash.c 原样照抄，只把"寄存器/总线"换成对数组偏移的操作。骨架如下（关键部分照抄 [flash.c:L68-L125](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c#L68-L125)，此处只示意替换点）：

     ```c
     /* sim_config.c —— 示例代码：PC 上模拟配置的存取与校验 */
     static union {                       /* 模拟 2KB Flash 页，保证 4 字节对齐 */
       uint8_t  bytes[0x800];
       uint32_t words[0x800 / 4];
     } flash_page;

     #define save_config_area 0           /* 模拟页内偏移从 0 开始 */

     void flash_erase_page_sim(void) { memset(flash_page.bytes, 0xFF, sizeof flash_page.bytes); }
     void flash_program_half_word_sim(uint32_t off, uint16_t d) {
       flash_page.bytes[off]   = d & 0xff;        /* 小端 */
       flash_page.bytes[off+1] = d >> 8;
     }
     /* checksum() / config_save() / config_recall() 照抄 flash.c，
        把 dst 指针换成 flash_page.bytes 内的偏移即可 */
     ```

   - `main()` 按顺序执行四个场景并打印结果：
     1. 上电未保存（先 `flash_erase_page_sim()` 模拟全新芯片）→ 调 `config_recall()`，预期返回 −1（magic 是 0xFFFFFFFF），RAM 里 `config` 仍是出厂默认值；
     2. 改 `config.uistat.freq = 7100000;` → `config_save()` → 再 `config_recall()`，预期返回 0、频率读回一致；
     3. **改坏一个数据字节**：`flash_page.bytes[100] ^= 0x01;`（落在 channels 区内，避开 checksum 字段本身）→ `config_recall()`，观察返回值；
     4. 改坏 magic 首字节：`flash_page.bytes[0] ^= 0x01;` → `config_recall()`，观察第一道关卡先拦下。
3. **需要观察的现象**：四个场景各自打印的返回值，以及每次失败后 RAM 副本 `config.uistat.freq` 是否仍保持上一次的有效值（出厂默认或上次保存值）。
4. **预期结果**：场景 1、3、4 均返回 −1；场景 2 返回 0。场景 3 里单字节翻转必然使整块 XOR ≠ 0 被检出（一个字变了，\( s' = w_i \oplus w_i' \neq 0 \)）；场景 4 连 XOR 都不用算，magic 直接不匹配。每次失败后 RAM 副本完好——这正是真机上"回到出厂默认、机器照样能开机"的机制。**待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：如果把保存时的"先清零再计算"两行颠倒——先算校验和、再把它写进字段——`config_recall` 的零判据还成立吗？

答案：不成立。若 checksum 字段还带着上一次的旧值 \( c_{old} \) 参与计算，写入的是 \( s = \mathrm{rest} \oplus c_{old} \)，读取时整块异或得到 \( \mathrm{rest} \oplus s = c_{old} \)，只有碰巧 \( c_{old}=0 \) 才为 0。"清零 → 计算 → 回填"这个次序是零判据成立的必要条件。

**练习 2**：这套校验能否发现"两个字节的损坏恰好互相抵消"？

答案：可能发现不了。XOR 校验等价于对每个二进制位列独立做奇偶校验：两个损坏若在完全相同的位集合上翻转（异或增量相同），整块异或结果不变，漏检。单字节损坏、奇数处损坏必然检出；对抗随机多位错误要靠 CRC。对"写一半断电"这种典型故障（后半段全是 0xFF），XOR 足够。

**练习 3**：`config_recall()` 为什么不像 `config_save()` 那样需要 `flash_unlock()`？

答案：解锁只保护**写操作**（擦除/编程）。`config_recall` 只读——`src->magic` 和 `checksum(src, ...)` 都是普通读访问，读 Flash 永远不需要钥匙。这也解释了为什么解锁只出现在 `config_save()` 和 `clear_all_config_prop_data()` 的开头。

### 4.4 存在哪里、何时存：0x0801f800 的选址与保存触发链

#### 4.4.1 概念说明

选址逻辑一句话：**代码从 Flash 低地址往上长，配置放最后一页，中间留出尽可能大的空隙**。但要看懂"这为什么安全（以及它有多脆弱）"，必须对照链接脚本——这是本讲第三个学习目标。

保存的触发时机在产品上也有讲究：擦一页要忙等毫秒级，期间同 bank 的取指都会停顿（数据手册行为），所以固件把保存动作**只绑在两个用户显式动作**上：shell 的 `save` 命令、旋钮长按（先蜂鸣提示再落盘）。绝不在后台悄悄保存。

#### 4.4.2 核心流程

保存触发的两条路径汇到同一个函数：

```
路径 A：USB shell                    路径 B：面板操作（ui.c）
  cmd_save (main.c:819)                ui_process (ui.c:261-264)
    │ config.uistat = uistat;            │ 长按 ≥1.6s → 蜂鸣提示
    └───────────┬────────────────────────┘
                ▼
     save_config_current_channel (main.c:247)
       ① config.channels[当前信道] = 当前频率/模式
       ② config.uistat = uistat
       ③ config_save()  → 擦 0x0801f800 → 写 444 半字
```

擦除一切的路径（危险操作，需密钥）：

```
clearconfig 1234 → cmd_clearconfig (main.c:830)
  → clear_all_config_prop_data (flash.c:127) → 解锁 → 擦配置页
```

#### 4.4.3 源码精读

先看地址常量与页大小，[flash.c:L80-L84](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c#L80-L84)：

```c
#define FLASH_PAGESIZE 0x800

// last page of flash memory. assume STM32F303CBT6 flash 128k Device
const uint32_t save_config_area = 0x0801f800;
const uint32_t save_config_prop_area_size = 0x800;
```

再对照链接脚本 [STM32F303xB.ld:L20-L38](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/STM32F303xB.ld#L20-L38)：

```
MEMORY
{
    flash0  : org = 0x08000000, len = 128k
    ...
    ram0    : org = 0x20000000, len = 40k
    ram4    : org = 0x10000000, len = 8k
}
```

**关键观察**：`flash0` 被声明为**完整的 128KB**，且后续 `REGION_ALIAS("TEXT_FLASH", flash0)` 等别名（[STM32F303xB.ld:L43-L61](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/STM32F303xB.ld#L43-L61)）让所有代码段、只读数据都从这 128KB 里分配。链接脚本里**没有任何一行**为配置页做保留——没有专门的 section、没有把 len 改成 126k。

也就是说，`0x0801f800` 这页的安全**不靠机制、只靠约定**：

- 现状安全，是因为固件体积（text+data，u1-l2 里你用 `arm-none-eabi-size` 看过）远小于 126KB，链接器自然没把代码排进最后一页；
- 一旦某天固件膨胀越过 126KB，链接器会**毫无警告地**把代码排进 `0x0801f800`；第一次长按保存，`config_save()` 的页擦除就会把那段代码擦掉——轻则功能异常、重则变砖，而且"按一下保存键才坏"这种故障极难排查。

这就是为什么 u1-l2 把"构建后用 size 核对容量"列为固定动作：128KB 的表称容量里，**真正可用的是 126KB**。稳妥的改进是把链接脚本的 `flash0` len 改为 `126k`，让越界变成链接期错误而不是运行期自毁（可作为读者思考题）。

保存触发链的证据。[main.c:L830-L844](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L830-L844) 的 `clearconfig` 用硬编码密钥 `1234` 做防误触（擦除不可逆，会清光 100 个信道）：

```c
  if (strcmp(argv[0], "1234") != 0) {
    chprintf(chp, "Key unmatched.\r\n");
    return;
  }
  clear_all_config_prop_data();
```

[ui.c:L261-L264](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L261-L264) 是长按保存（u4-l4 讲过的 `EVT_BUTTON_DOWN_LONG`）：

```c
    } else if (status & EVT_BUTTON_DOWN_LONG) {
      tlv320aic3204_beep();            // 先"哔"一声告知
      save_config_current_channel();
    }
```

[ui.c:L209-L219](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L209-L219) 的 `recall_channel()` 展示信道库的消费方式——只从 RAM 镜像读，与 Flash 无关：

```c
void
recall_channel(unsigned int channel)
{
  uistat.freq = config.channels[channel].freq;
  uistat.modulation = config.channels[channel].modulation;
  set_modulation(uistat.modulation);
  update_frequency();
}
```

#### 4.4.4 代码实践

1. **实践目标**：把"保存配置"对实时系统的冲击变成可观测的数据，理解为什么保存只放在用户显式动作之后。
2. **操作步骤**（有硬件时）：
   - 通过 USB shell 让设备处于 FM 立体声 192kHz 模式（回顾 u2-l3：此时解调回调周期只有 1.25ms）；
   - 用 `stat` 命令（u1-l4）先记一组 `load`/`fps` 基线；
   - 紧接着执行 `save`，同时连续观察 `stat` 输出——擦页的那几十毫秒里 `load` 会不会窜高、音频会不会出现瞬间打嗝；
   - 无硬件时改做纸面分析：`flash_erase_page()` 内的 `chSysLock()`（[flash.c:L43-L49](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c#L43-L49)）期间内核级切换被禁止、CPU 在 `BSY` 上自旋；本工程 I2S 中断优先级设为 2（[mcuconf.h:L167-L170](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/mcuconf.h#L167-L170)），高于内核屏蔽级的中断仍可抢占，但 Flash 忙期间同 bank 取指停顿是硬件行为，任何中断优先级都救不回被停顿的指令流。
3. **需要观察的现象**：`save` 前后 `load` 数值变化；是否听到/看到（波形模式下）音频流短暂中断。
4. **预期结果**：保存瞬间应观察到一次性的负载尖峰或短暂音频异常，随后恢复正常。这解释了产品设计：保存只发生在"用户长按旋钮并听到蜂鸣"这种预期之内的事件点，而不是后台周期性自动保存。具体幅度**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：把 `STM32F303xB.ld` 里的 `flash0 : org = 0x08000000, len = 128k` 改成 `len = 126k`，能解决什么问题？有没有代价？

答案：能让"固件体积超过 126KB"从**运行期自毁**（代码排进配置页、被 `config_save` 擦掉）变成**链接期报错**（region 溢出，构建直接失败）。代价：这属于预防性改进，本工程固件体积离 126KB 尚远，当前不构成实际问题；另外要记得 `0x0801f800` 这个常量与"最后一页"的对应关系就彻底交给人来维护了。这正是 u5-l3（链接脚本专题）要展开的主题。

**练习 2**：为什么 `save_config_current_channel()` 在写 Flash 前要先做 `config.channels[channel] = ...` 和 `config.uistat = uistat` 两个赋值，而不是让 `config_save` 直接去读 `uistat`？

答案：`uistat` 是 UI 状态机的**活现场**，`config` 是待落盘的**快照**，两者职责分离（`config` 还承载信道库、硬件修正量等 `uistat` 没有的数据）。`config_save()` 的职责被限定为"把 config 这个整体原样落盘"（顺带算校验和），不掺和对业务字段的采集。这也是为什么 `cmd_save`、`cmd_lcd`（`config.lcd_rotation = rot`，[main.c:L862-L872](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L862-L872)）、`cmd_revision` 都是先改 `config` 再等显式保存——所有路径遵循同一模式。

**练习 3**：长按保存前为什么先调 `tlv320aic3204_beep()` 蜂鸣？

答案：双向反馈。擦写会让系统卡一下、且 Flash 有寿命限制，保存不应是"悄悄发生"的事：蜂鸣告知用户"已收到保存指令"，随后即便出现短暂卡顿，用户也理解原因。小屏幕嵌入式设备拿音频通道做操作确认是常见手法（这里复用了编解码器的 beep 功能，见 u2-l2）。

## 5. 综合实践

**任务：给 `config_t` 新增一个 `backlight`（背光亮度）字段，并用 PC 模拟器完整走一遍"加字段 → 保存 → 召回 → 数据损坏回退"的全流程。**

这个任务把本讲四个模块串起来：改布局（4.2）、算校验和（4.3）、理解选址与触发（4.4）、体会 Flash 擦写模型（4.1）。

**步骤**：

1. **在模拟器上加字段**：在 4.3.4 的 `sim_config.c`（或 4.2.4 的 `layout.c`）里，把 `config_t` 定义改为在 `lcd_rotation` 之后、`checksum` 之前插入一行 `uint8_t backlight;`，先重新打印 `sizeof(config_t)` 与各 `offsetof`。
   - 预期：由于 `lcd_rotation` 后面本来就有 1 字节 padding（偏移 883），`backlight` 恰好填进旧 padding，**总大小很可能保持 888 不变**（待本地验证）。这是嵌入式结构体设计的经典现象：加一个 `uint8_t` 未必增加体积。
2. **验证新字段能存取**：`config.backlight = 200;` → `config_save()` → 把 RAM 副本清零 → `config_recall()` → 确认读回 200，且校验和判据依然为 0（布局变了，但"清零→计算→回填"的数学与字段内容无关）。
3. **制造"固件升级"场景（体会没有版本机制的后果）**：保持 Flash 里是**旧布局**（没有 backlight）保存的镜像，用**新布局**的 `config_recall()` 去读。预期：若旧镜像里偏移 883 的 padding 字节是 0x00（静态初始化的 padding 为零），backlight 读回 0；如果插入位置选在**结构体中间**（比如 `magic` 之后），后续所有字段整体错位、校验和必然不过、整个配置被当作损坏丢弃。
   - 结论：**新增字段必须追加在 checksum 之前、紧邻尾部**，且老用户首次升级后新字段读到的是默认值——本工程没有 `CONFIG_VERSION` 之类的版本号，这个约束纯粹靠开发者自律。
4. **（有硬件，选做）**：在真机上给 [nanosdr.h:L289-L299](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L289-L299) 的 `config_t` 加上该字段，找一个消费点（例如在 `main()` 里根据它调一次 DAC/背光相关接口），`make` 编译通过后烧录，用 `save` 命令保存、断电重启验证读回。改动只涉及头文件与初始化代码，不触碰本讲分析过的 flash.c 逻辑。

**验收标准**：模拟器四个场景（全新芯片回退、正常存取、单字节损坏拦截、magic 损坏拦截）全部复现；能口头回答"为什么新字段要加在末尾"和"为什么固件体积不能超过 126KB"。

## 6. 本讲小结

- STM32F303 写 Flash 的固定套路：`KEYR` 双钥匙解锁 → 置 `PER`、写 `AR`、置 `STRT` 页擦除 → 置 `PG` 后向目标地址做 16 位写访问完成半字编程，每步前后都自旋等 `SR.BSY` 清零。
- `flash_erase_page()` 用 `chSysLock()` 包住"选类型→设地址→启动"的序列防并发插队，而单次半字编程不加锁——长短操作区别对待。
- `config_t`（推算 888 字节 = 100 个信道 800 字节 + AGC 28 字节 + uistat 44 字节 + 头尾校验与修正量）整体落在 Flash 最后一页 2KB 里，RAM 镜像 `config` 与 Flash 镜像经 `memcpy` 互拷，因为 Flash 数据不能就地修改。
- 完整性靠两道关卡：`magic` 必须是 'CONF'（`0x434f4e45`）；整块含 checksum 字段的 XOR 必须为 0——由自反律 \( x \oplus x = 0 \) 保证，任一单处字节损坏必然被检出，读取失败时固件静默回退到 main.c 的出厂默认 `config`。
- `0x0801f800` 是 128KB Flash 的最后一页，但链接脚本仍把 `flash0` 声明为完整 128KB——代码与配置页不冲突**只靠"固件小于 126KB"这一约定**，没有机制兜底。
- 保存只在两个用户显式动作触发（`save` 命令、长按旋钮先蜂鸣后落盘）；`channel save` 只改 RAM 镜像，把擦写次数留给最终的整块保存。

## 7. 下一步学习建议

本讲是单元四（显示、UI 与配置）的最后一讲。接下来进入单元五（专家层），与本讲衔接最紧的两讲：

- **u5-l1 并发与实时**：本讲留下了"擦页期间 `chSysLock` + BSY 自旋对 1.25ms 硬实时音频流意味着什么"的问题，那一讲用 DWT 计数器与 `stat` 的负载指标系统性地回答。
- **u5-l3 链接脚本与内存布局**：本讲只读了 `MEMORY` 里的一行 `flash0`；那一讲展开 `rules_code.ld`、CCM 紧耦合内存与段重排，并练习把"配置页保留"真正做进链接脚本。

继续深挖的建议阅读顺序：重读 [flash.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c)（140 行，现在应当能逐行讲出每一行存在的理由）→ STM32F3 参考手册（RM0316）的 Flash 编程章节核对 `KEYR`/`CR`/`SR`/`AR` 各位的定义与页擦除时序参数 → 对照 u5-l4（扩展点）思考：如果要做"配置双页轮流写以摊薄擦写损耗"，flash.c 的哪些函数签名需要变。
