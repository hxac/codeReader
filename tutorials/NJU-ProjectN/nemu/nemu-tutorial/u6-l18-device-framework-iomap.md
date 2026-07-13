# 设备框架与 IOMap 映射

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 NEMU 用什么数据结构把一段客机地址「绑定」到一个设备上，以及这个绑定在运行时如何被查到。
- 解释 `map_read`/`map_write` 的「数据缓冲 + 回调」双路径设计，特别是**读先回调、写后回调**这一非对称时序背后的原因。
- 掌握内存映射 I/O（MMIO）与端口 I/O（PIO）两套映射机制的异同，知道它们分别由谁驱动。
- 画出从 `paddr_write` 一路到设备回调函数的完整调用链。
- 解释为什么 MMIO 区域既不能与物理内存 `pmem` 重叠，也不能彼此重叠（`report_mmio_overlap`）。
- 理解 `init_device` 在启动时如何把所有设备装配起来。

本讲承接 u4-l12（物理内存 paddr）。在 u4-l12 中，`paddr_read`/`paddr_write` 做的是「pmem → mmio → 越界」三分支路由，但当时把 mmio 分支当作黑盒。本讲就打开这个黑盒。

## 2. 前置知识

在进入源码前，先建立两个直觉。

### 2.1 真实硬件里「设备」是怎么被 CPU 访问的

CPU 只会做两类事：算逻运算、读写内存。那么它怎么「按键」「画屏」「读时钟」？答案是：把设备暴露成一段「地址」。CPU 往某个地址写一个字节，硬件就把它解释成「往串口送一个字符」；CPU 从某个地址读一个字，硬件就返回「当前时间戳」。

这种「用访问内存的指令去访问设备」的方式叫**内存映射 I/O（Memory-Mapped I/O，MMIO）**。还有一种更古老的方式：CPU 有专门的 `in`/`out` 指令，访问一个独立的 16 位「端口地址空间」，这叫**端口 I/O（Port I/O，PIO）**，x86 特有。RISC-V 没有 PIO，只有 MMIO。

所以「设备」在 CPU 眼里就是一段地址区间：往这段区间里的某个偏移读/写，就会触发设备行为。

### 2.2 NEMU 为什么要一套「框架」

NEMU 支持串口、定时器、键盘、VGA、音频、磁盘、SD 卡七种设备，还要同时支持 MMIO 和 PIO 两种映射方式。如果每个设备自己处理「地址查找、越界检查、字节宽度、副作用回调」，代码会大量重复。

NEMU 的做法是抽出一个**通用 IOMap 框架**：每个设备只需提供「一段缓冲区 + 一个回调函数」，框架负责地址路由、越界检查、字节读写。设备作者只关心「这个偏移被读/写时我该做什么」。

关键术语速查：

| 术语 | 含义 |
|------|------|
| IOMap | 一个设备在地址空间里的映射条目（名字 + 区间 + 缓冲 + 回调） |
| MMIO | 内存映射 I/O，设备挂在物理地址总线上，由 `paddr_read/write` 分流 |
| PIO | 端口 I/O，x86 专用，由 `in`/`out` 指令经 `pio_read/write` 驱动 |
| `io_callback_t` | 设备回调函数类型 `void (*)(uint32_t offset, int len, bool is_write)` |
| `io_space` | 所有设备缓冲区共享的一块 32MB 内存池 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/device/map.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/device/map.h) | IOMap 结构定义、`map_inside`/`find_mapid_by_addr` 内联函数、`add_mmio_map`/`add_pio_map`/`map_read`/`map_write` 声明 |
| [src/device/io/map.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/map.c) | IOMap 框架的实现：`new_space` 缓冲池分配、`check_bound` 越界检查、`invoke_callback`、`map_read`/`map_write`、`init_map` |
| [src/device/io/mmio.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/mmio.c) | MMIO 侧：`maps[]` 表、`add_mmio_map`（含重叠检测）、`fetch_mmio_map`、`mmio_read`/`mmio_write` |
| [src/device/io/port-io.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/port-io.c) | PIO 侧：独立的 `maps[]` 表、`add_pio_map`、`pio_read`/`pio_write` |
| [src/device/device.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c) | 设备装配入口 `init_device`、每步轮询 `device_update` |
| [src/memory/paddr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c) | 物理总线 `paddr_read`/`paddr_write`，把非 pmem 访问分流到 `mmio_read`/`mmio_write` |

辅助理解的设备实例：[src/device/serial.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/serial.c)（写后回调样板）、[src/device/timer.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/timer.c)（读前回调样板）。

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：IOMap 结构与地址查找、`map_read`/`map_write` 回调时序、`add_mmio_map` 与重叠检测、`add_pio_map` 与端口 I/O、`init_device` 装配与 paddr 分流。

### 4.1 IOMap 结构与地址区间查找

#### 4.1.1 概念说明

「把一段地址绑定到一个设备」这句话，落到代码里就是一个结构体 `IOMap`。它要回答四个问题：

1. 这段映射叫什么名字？（排错用）
2. 它占哪段地址区间？（`low` 到 `high`，闭区间）
3. 真正存数据的是哪块内存？（`space` 指针）
4. 访问这段地址时要不要通知设备？（`callback`）

运行时，CPU 给出一个地址，框架要在所有已注册的 `IOMap` 里找到包含它的那一条——这就是 `find_mapid_by_addr` 的工作。

#### 4.1.2 核心流程

设备注册与查找的关系如下：

```
启动期：init_device → init_serial() → new_space(8) 分配缓冲
                                  └→ add_mmio_map("serial", 0xa00003f8, base, 8, cb)
                                        └→ 填入 maps[nr_map++]

运行期：CPU 访问 addr=0xa00003f8
        paddr_write → mmio_write → fetch_mmio_map(addr)
                                     └→ find_mapid_by_addr(maps, nr_map, addr)
                                          └→ 遍历 maps[]，map_inside 命中 → 返回下标
```

#### 4.1.3 源码精读

IOMap 结构定义在 [include/device/map.h:24-31](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/device/map.h#L24-L31)，这段代码定义了设备映射的核心数据结构：

```c
typedef struct {
  const char *name;
  // we treat ioaddr_t as paddr_t here
  paddr_t low;
  paddr_t high;
  void *space;
  io_callback_t callback;
} IOMap;
```

- `low`/`high` 是闭区间 `[low, high]`，注意「闭」——`high = addr + len - 1`，两端都算有效。
- `space` 指向设备自己的缓冲区（由 `new_space` 从公共池里切出）。
- `callback` 是设备提供的回调，可空（如 VGA 控制寄存器 `vgactl` 注册时传 `NULL`）。
- 注释「we treat ioaddr_t as paddr_t here」说明 PIO 侧也复用这个结构，把 16 位端口地址塞进 `paddr_t` 字段。

`map_inside` 判断地址是否落在某条映射内，见 [include/device/map.h:33-35](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/device/map.h#L33-L35)，即简单的闭区间判定 `addr >= map->low && addr <= map->high`。

`find_mapid_by_addr` 是运行时查找的核心，见 [include/device/map.h:37-46](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/device/map.h#L37-L46)：

```c
static inline int find_mapid_by_addr(IOMap *maps, int size, paddr_t addr) {
  int i;
  for (i = 0; i < size; i ++) {
    if (map_inside(maps + i, addr)) {
      difftest_skip_ref();
      return i;
    }
  }
  return -1;
}
```

这里有一个关键细节：**一旦命中，立即调用 `difftest_skip_ref()`**。原因是设备访问有副作用（按键、画屏、读时钟），参考实现 REF（如 QEMU/spike）无法重现同样的副作用，所以差分测试必须跳过这一步的寄存器比对（详见 u8-l24）。这是「设备有副作用」这一性质在代码里的直接体现。

注意查找是**线性扫描**，`NR_MAP` 只有 16，设备数量极少，无需哈希或树。

缓冲区由 `new_space` 从公共池 `io_space` 切出，见 [src/device/io/map.c:26-33](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/map.c#L26-L33)：

```c
uint8_t* new_space(int size) {
  uint8_t *p = p_space;
  // page aligned;
  size = (size + (PAGE_SIZE - 1)) & ~PAGE_MASK;
  p_space += size;
  assert(p_space - io_space < IO_SPACE_MAX);
  return p;
}
```

这是一个经典的 **bump allocator（碰撞分配器）**：`p_space` 是个游标，每次分配就返回当前游标并向前推进，按页对齐。永不回收，但设备缓冲区在程序生命周期内常驻，无需回收。`IO_SPACE_MAX` 是 32MB（[map.c:21](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/map.c#L21)），远大于实际需求。

> 端口地址类型 `ioaddr_t` 定义在 [include/common.h:45](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L45)，为 `uint16_t`，对应 x86 64K 的独立端口地址空间。

#### 4.1.4 代码实践

**实践目标**：理解一个设备如何注册自己的 IOMap。

**操作步骤**：

1. 打开 [src/device/serial.c:43-51](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/serial.c#L43-L51) 的 `init_serial`。
2. 对照 `add_mmio_map` 的签名 `add_mmio_map(name, addr, space, len, callback)`，把五个实参一一对应：
   - `name = "serial"`
   - `addr = CONFIG_SERIAL_MMIO`（默认 `0xa00003f8`，见 [src/device/Kconfig:25-27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/Kconfig#L25-L27)）
   - `space = serial_base`（由 `new_space(8)` 分配）
   - `len = 8`
   - `callback = serial_io_handler`
3. 在 [src/device/io/mmio.c:36-54](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/mmio.c#L36-L54) 的 `add_mmio_map` 里，确认这五个值如何被填进 `maps[nr_map]`，以及 `high = addr + len - 1 = 0xa00003ff`。

**需要观察的现象**：`name` 是 `const char*` 字符串字面量，`space` 是堆上的 8 字节缓冲，二者职责完全不同——前者只供日志/排错，后者才是真正存数据的地方。

**预期结果**：能在纸上写出 serial 这条 IOMap 的 `{name, low, high, space, callback}` 五元组。

#### 4.1.5 小练习与答案

**练习 1**：`find_mapid_by_addr` 为什么用线性遍历而不是二分或哈希？

**答案**：`NR_MAP` 上限只有 16，设备数量极少，线性遍历的常数开销可忽略；且映射在启动期一次性注册、运行期只读不改，没有必要为加速查找引入更复杂的数据结构。代码简单优先，这是 NEMU 一贯的教学取舍。

**练习 2**：`find_mapid_by_addr` 命中时调用 `difftest_skip_ref()`，如果漏调会发生什么？

**答案**：设备访问有副作用且与宿主时间相关（如 RTC 读主机时间），REF 无法重现同样的设备状态与寄存器变化，差分比对会在这一步误报「寄存器不一致」，把正常的设备访问当成 bug。所以命中设备映射必须跳过 REF 比对。

### 4.2 map_read / map_write 的回调时序

#### 4.2.1 概念说明

找到 `IOMap` 之后，真正读写由 `map_read`/`map_write` 完成。这里藏着本讲最精妙的设计：

设备的数据传输分两条路径——

1. **数据路径**：`space` 缓冲区与 CPU 之间搬字节，由 `host_read`/`host_write` 完成（复用 u4-l12 讲过的宿主指针按宽度解引用）。
2. **控制路径**：`callback` 通知设备「有人访问你了」，让设备做出反应（输出字符、读时钟、置中断等）。

两条路径的**先后顺序对读和写是相反的**，这是理解整个设备框架的关键。

#### 4.2.2 核心流程

```
map_read(addr, len, map):         map_write(addr, len, data, map):
  1. assert len ∈ [1,8]              1. assert len ∈ [1,8]
  2. check_bound(map, addr)          2. check_bound(map, addr)
  3. offset = addr - map->low        3. offset = addr - map->low
  4. callback(offset, len, false)  ←─ 先回调：让设备「准备」要读的数据
  5. ret = host_read(space+offset)   4. host_write(space+offset, data) ←─ 先写缓冲
  6. return ret                      5. callback(offset, len, true)   ←─ 后回调：让设备「反应」
```

读：**先回调后读缓冲**——因为设备要先准备好数据（如把当前时间写进缓冲），CPU 才能读到正确值。
写：**先写缓冲后回调**——因为设备要先看到写入的值（字符已落在缓冲里），才能据此反应（把字符输出到 stderr）。

#### 4.2.3 源码精读

`map_read` 见 [src/device/io/map.c:55-62](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/map.c#L55-L62)，注意回调在 `host_read` 之前：

```c
word_t map_read(paddr_t addr, int len, IOMap *map) {
  assert(len >= 1 && len <= 8);
  check_bound(map, addr);
  paddr_t offset = addr - map->low;
  invoke_callback(map->callback, offset, len, false); // prepare data to read
  word_t ret = host_read(map->space + offset, len);
  return ret;
}
```

`map_write` 见 [src/device/io/map.c:64-70](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/map.c#L64-L70)，注意回调在 `host_write` 之后：

```c
void map_write(paddr_t addr, int len, word_t data, IOMap *map) {
  assert(len >= 1 && len <= 8);
  check_bound(map, addr);
  paddr_t offset = addr - map->low;
  host_write(map->space + offset, len, data);
  invoke_callback(map->callback, offset, len, true);
}
```

注释 `// prepare data to read` 一语道破读路径的设计意图。`invoke_callback` 只是个 NULL 守卫，见 [src/device/io/map.c:45-47](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/map.c#L45-L47)：`if (c != NULL) { c(offset, len, is_write); }`，所以注册时传 `NULL` 的设备（如 `vgactl`）就只有数据路径、没有控制路径。

`check_bound` 做越界断言，见 [src/device/io/map.c:35-43](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/map.c#L35-L43)。注意它校验的是**起始地址 `addr`** 是否落在 `[low, high]`，并在失败时打印设备名、区间与当前 `cpu.pc`，是排错第一现场：

```c
static void check_bound(IOMap *map, paddr_t addr) {
  if (map == NULL) {
    Assert(map != NULL, "address (" FMT_PADDR ") is out of bound at pc = " FMT_WORD, addr, cpu.pc);
  } else {
    Assert(addr <= map->high && addr >= map->low,
        "address (" FMT_PADDR ") is out of bound {%s} [" FMT_PADDR ", " FMT_PADDR "] at pc = " FMT_WORD,
        addr, map->name, map->low, map->high, cpu.pc);
  }
}
```

`map == NULL` 分支处理「地址不在任何已注册映射内」的情况（`fetch_mmio_map` 找不到时返回 NULL）。

**两个真实设备对照**：

- **写后回调样板——串口**。`serial_io_handler` 见 [src/device/serial.c:31-41](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/serial.c#L31-L41)：当 `is_write` 且 `offset==0` 时，从 `serial_base[0]` 取出字符输出到 stderr。这依赖 `map_write` 已经先把字符写进 `serial_base[0]`，回调才能读到——印证「写先缓冲后回调」。

```c
static void serial_io_handler(uint32_t offset, int len, bool is_write) {
  assert(len == 1);
  switch (offset) {
    case CH_OFFSET:
      if (is_write) serial_putc(serial_base[0]);
      else panic("do not support read");
      break;
    default: panic("do not support offset = %d", offset);
  }
}
```

- **读前回调样板——定时器 RTC**。`rtc_io_handler` 见 [src/device/timer.c:22-29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/timer.c#L22-L29)：当 `!is_write && offset==4` 时，读宿主时间 `get_time()` 并写进 `rtc_port_base[0/1]`。这依赖 `map_read` 在 `host_read` 之前先调用回调，回调把时间填进缓冲，随后 `host_read` 才能读到——印证「读先回调后缓冲」。

```c
static void rtc_io_handler(uint32_t offset, int len, bool is_write) {
  assert(offset == 0 || offset == 4);
  if (!is_write && offset == 4) {
    uint64_t us = get_time();
    rtc_port_base[0] = (uint32_t)us;
    rtc_port_base[1] = us >> 32;
  }
}
```

这两个例子正好对称：串口是「CPU 写→设备输出」，RTC 是「设备准备→CPU 读」。把它们放在一起，就理解了为什么读和写的回调时序必须相反。

#### 4.2.4 代码实践

**实践目标**：用「读前回调」与「写后回调」两个样板验证时序设计。

**操作步骤**：

1. 阅读串口的写路径：CPU 执行 `sb` 往 `0xa00003f8` 写一个字符 →（按 4.5 的链路）→ `map_write` 先 `host_write` 落字符到 `serial_base[0]` → 再 `invoke_callback(..., true)` → `serial_io_handler` 调 `serial_putc` 输出到 stderr。
2. 阅读 RTC 的读路径：CPU 执行 `lw` 从 `0xa000004c`（即 `CONFIG_RTC_MMIO + 4`）读时间 → `map_read` 先 `invoke_callback(..., false)` → `rtc_io_handler` 把 `get_time()` 填进 `rtc_port_base` → 再 `host_read` 读出。
3. 思考：如果把 `map_read` 里的回调移到 `host_read` 之后，RTC 还能读对吗？

**需要观察的现象**：串口写时字符能被输出，是因为回调发生时缓冲里已有数据；RTC 读时能拿到当前时间，是因为回调先于读取发生。

**预期结果**：能口述出「串口依赖写后回调、RTC 依赖读前回调」，并解释调换顺序会破坏哪一个。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `map_read` 的 `assert(len >= 1 && len <= 8)` 把宽度限制在 8 字节内？

**答案**：CPU 单条访存指令最多读一个机器字，NEMU 中 `word_t` 最大 8 字节（RV64）；设备寄存器也都是 1/2/4/8 字节宽。超过 8 字节的访问不属于单次总线事务，框架无需支持。

**练习 2**：`check_bound` 只校验起始地址 `addr`，不校验 `addr+len-1`，这会带来什么潜在问题？

**答案**：若 `addr` 落在某个映射的末尾附近（如 `addr == high`），`host_read/write(space+offset, len)` 实际读写的字节会越过 `high`、读到相邻区域。由于 `io_space` 是连续大池且设备缓冲按页对齐，越界不会立即崩溃，但会读到不属于本设备的字节。NEMU 设计时假定设备访问都对齐到寄存器边界、不会跨出映射，所以未做末端校验——这是一个教学取舍下的简化。

### 4.3 add_mmio_map 与重叠检测

#### 4.3.1 概念说明

`add_mmio_map` 是设备注册到 MMIO 总线的入口。除了填表，它还承担一项关键职责：**检测地址重叠**。

为什么不能重叠？因为 `paddr_read`/`paddr_write` 是按地址路由的：先判 `in_pmem`，否则进 `mmio_read`。如果一段地址既是 pmem 又是某设备的 MMIO，路由会二义；如果两个设备 MMIO 区间互相重叠，`find_mapid_by_addr` 只会返回第一个命中的，另一个设备永远收不到访问——这是隐蔽的配置错误。NEMU 选择在注册时就把这种错误 panic 掉，而不是等到运行时出现诡异行为。

#### 4.3.2 核心流程

```
add_mmio_map(name, addr, space, len, cb):
  1. assert(nr_map < NR_MAP)            # 表未满（NR_MAP=16）
  2. left=addr, right=addr+len-1
  3. if in_pmem(left) || in_pmem(right) → report_mmio_overlap(... "pmem" ...)   # 不能压在 pmem 上
  4. for 已有 maps[i]: if left<=maps[i].high && right>=maps[i].low → report_mmio_overlap(...)  # 不能互相压
  5. maps[nr_map] = {name, left, right, space, cb}
  6. Log(...) ; nr_map++
```

第 3 步用 `in_pmem`（u4-l12 讲过的无符号减法区间判定）检查端点是否落在物理内存里；第 4 步用「左端 ≤ 对方上界 且 右端 ≥ 对方下界」这一标准区间相交判据，检查与已有映射是否冲突。

#### 4.3.3 源码精读

`add_mmio_map` 见 [src/device/io/mmio.c:36-54](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/mmio.c#L36-L54)：

```c
void add_mmio_map(const char *name, paddr_t addr, void *space, uint32_t len, io_callback_t callback) {
  assert(nr_map < NR_MAP);
  paddr_t left = addr, right = addr + len - 1;
  if (in_pmem(left) || in_pmem(right)) {
    report_mmio_overlap(name, left, right, "pmem", PMEM_LEFT, PMEM_RIGHT);
  }
  for (int i = 0; i < nr_map; i++) {
    if (left <= maps[i].high && right >= maps[i].low) {
      report_mmio_overlap(name, left, right, maps[i].name, maps[i].low, maps[i].high);
    }
  }

  maps[nr_map] = (IOMap){ .name = name, .low = addr, .high = addr + len - 1,
    .space = space, .callback = callback };
  Log("Add mmio map '%s' at [" FMT_PADDR ", " FMT_PADDR "]",
      maps[nr_map].name, maps[nr_map].low, maps[nr_map].high);

  nr_map ++;
}
```

注意端点检查用的是 `in_pmem(left) || in_pmem(right)`——只查两端，不查中间。这足以拦截「整段落在 pmem 内」和「端点压在 pmem 边界」的常见情况；对于「两端都在 pmem 外但中间穿过 pmem」的极端跨区间情况未覆盖，但实际设备区间都很小且连续，不会出现这种穿越。

`report_mmio_overlap` 见 [src/device/io/mmio.c:29-33](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/mmio.c#L29-L33)，直接 `panic` 终止，打印两个冲突区域的名字与区间，便于定位配置错误：

```c
static void report_mmio_overlap(const char *name1, paddr_t l1, paddr_t r1,
    const char *name2, paddr_t l2, paddr_t r2) {
  panic("MMIO region %s@[" FMT_PADDR ", " FMT_PADDR "] is overlapped "
               "with %s@[" FMT_PADDR ", " FMT_PADDR "]", name1, l1, r1, name2, l2, r2);
}
```

MMIO 侧的映射表与查找函数见 [src/device/io/mmio.c:19-27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/mmio.c#L19-L27)：

```c
#define NR_MAP 16
static IOMap maps[NR_MAP] = {};
static int nr_map = 0;

static IOMap* fetch_mmio_map(paddr_t addr) {
  int mapid = find_mapid_by_addr(maps, nr_map, addr);
  return (mapid == -1 ? NULL : &maps[mapid]);
}
```

`fetch_mmio_map` 找不到时返回 `NULL`，由 `map_read`/`map_write` 里的 `check_bound(NULL, ...)` 兜住并 panic。

最终对外的总线接口 `mmio_read`/`mmio_write` 见 [src/device/io/mmio.c:57-63](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/mmio.c#L57-L63)，把「查找 + 读写」串起来：

```c
word_t mmio_read(paddr_t addr, int len) {
  return map_read(addr, len, fetch_mmio_map(addr));
}
void mmio_write(paddr_t addr, int len, word_t data) {
  map_write(addr, len, data, fetch_mmio_map(addr));
}
```

#### 4.3.4 代码实践

**实践目标**：亲手触发一次 `report_mmio_overlap`，理解重叠检测的意义。

**操作步骤**（仅阅读与分析，不修改源码）：

1. 查看 [src/device/Kconfig:25-27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/Kconfig#L25-L27) 与 [src/device/Kconfig:44-46](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/Kconfig#L44-L46)，确认 serial 默认 `0xa00003f8`、rtc 默认 `0xa0000048`，两者区间 `[0xa00003f8,0xa00003ff]` 与 `[0xa0000048,0xa000004f]` 不重叠。
2. 假设在 `make menuconfig` 里把 `CONFIG_RTC_MMIO` 改成 `0xa00003f8`（与 serial 同址），推演会发生什么：`add_mmio_map("rtc", 0xa00003f8, ...)` 时，`left=0xa00003f8`、`right=0xa00003ff`，与已注册的 serial `maps[0] = {low=0xa00003f8, high=0xa00003ff}` 满足 `left <= maps[0].high && right >= maps[0].low` → `report_mmio_overlap("rtc", ..., "serial", ...)` → `panic`。
3. 再推演一个与 pmem 重叠的例子：若把某设备 MMIO 地址设为 `0x80000000`（即 `CONFIG_MBASE` 默认值），`in_pmem(left)` 为真 → panic 报「与 pmem 重叠」。

**需要观察的现象**：NEMU 在 `init_device` 阶段、注册到冲突设备时立即崩溃，并打印双方名字与区间，而非运行时静默错误。

**预期结果**：能解释「重叠必须在注册期 panic，否则运行期 `find_mapid_by_addr` 只会命中先注册者，后注册的设备永远收不到访问，问题极难排查」。

> 说明：本实践为源码阅读型推演，未实际运行；如需运行验证，可在 `make menuconfig` 中改动 MMIO 地址后 `make run` 观察启动期 panic 输出。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`add_mmio_map` 用 `in_pmem(left) || in_pmem(right)` 检查与 pmem 重叠，为什么只查端点？

**答案**：设备 MMIO 区间通常很小（8 字节居多）且连续，只要两端都不在 pmem 内，整段就不会落在 pmem 中；逐字节检查整段开销大且无必要。端点检查覆盖了所有实际会出现的配置错误。

**练习 2**：区间相交判据 `left <= maps[i].high && right >= maps[i].low` 是否正确？设 A=[2,5]、B=[6,8] 会误报吗？

**答案**：正确且不会误报。代入 A.left=2, A.right=5, B.low=6, B.high=8：`2 <= 8 && 5 >= 6` → `true && false` → false，判定不相交，正确。该判据是标准的闭区间相交条件：`max(low) <= min(high)` 的等价变形。

### 4.4 add_pio_map 与端口 I/O

#### 4.4.1 概念说明

PIO（端口 I/O）是 x86 特有的设备访问方式：CPU 用 `in`/`out` 指令访问一个**独立的 16 位地址空间**（0–65535），与物理内存地址空间完全隔离。RISC-V 没有 PIO。

NEMU 用同一套 IOMap 框架同时支持 MMIO 和 PIO，区别只在于：

- MMIO 设备挂在物理地址总线上，由 `paddr_read/write` 分流，地址是 `paddr_t`。
- PIO 设备挂在独立的端口空间里，由 x86 `in`/`out` 指令经 `pio_read/write` 驱动，地址是 `ioaddr_t`（`uint16_t`）。

两者各有一张 `maps[]` 表，互不干扰。`CONFIG_HAS_PORT_IO` 仅在 `ISA_x86` 时为 `y`（见 [src/device/Kconfig:10-13](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/Kconfig#L10-L13)）。

#### 4.4.2 核心流程

每个设备在注册时二选一：

```
init_serial():
  new_space(8) → serial_base
  if CONFIG_HAS_PORT_IO:  add_pio_map ("serial", CONFIG_SERIAL_PORT,  ...)   # x86: 0x3f8
  else:                   add_mmio_map("serial", CONFIG_SERIAL_MMIO, ...)   # riscv: 0xa00003f8
```

PIO 运行期路径（由 x86 `in`/`out` 指令实现调用，当前为 PA 待实现部分）：

```
pio_read(addr, len) → find_mapid_by_addr(maps, nr_map, addr) → map_read(...)
pio_write(addr, len, data) → find_mapid_by_addr(...) → map_write(...)
```

#### 4.4.3 源码精读

`add_pio_map` 见 [src/device/io/port-io.c:25-34](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/port-io.c#L25-L34)：

```c
void add_pio_map(const char *name, ioaddr_t addr, void *space, uint32_t len, io_callback_t callback) {
  assert(nr_map < NR_MAP);
  assert(addr + len <= PORT_IO_SPACE_MAX);
  maps[nr_map] = (IOMap){ .name = name, .low = addr, .high = addr + len - 1,
    .space = space, .callback = callback };
  Log("Add port-io map '%s' at [" FMT_PADDR ", " FMT_PADDR "]",
      maps[nr_map].name, maps[nr_map].low, maps[nr_map].high);

  nr_map ++;
}
```

对比 `add_mmio_map`，PIO 侧有两个显著差异：

1. **没有重叠检测**。因为端口空间与 pmem 完全隔离，不存在「压在 pmem 上」的问题；且 x86 标准端口地址（serial 0x3f8、rtc 0x48、kbd 0x60、vgactl 0x100、audio 0x200、disk 0x300）由 Kconfig 默认值保证互不冲突，框架信任配置。
2. **有端口空间上界校验**：`assert(addr + len <= PORT_IO_SPACE_MAX)`，`PORT_IO_SPACE_MAX = 65535`（[port-io.c:18](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/port-io.c#L18)），对应 `ioaddr_t` 是 16 位。

CPU 侧接口 `pio_read`/`pio_write` 见 [src/device/io/port-io.c:37-49](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/port-io.c#L37-L49)：

```c
uint32_t pio_read(ioaddr_t addr, int len) {
  assert(addr + len - 1 < PORT_IO_SPACE_MAX);
  int mapid = find_mapid_by_addr(maps, nr_map, addr);
  assert(mapid != -1);
  return map_read(addr, len, &maps[mapid]);
}

void pio_write(ioaddr_t addr, int len, uint32_t data) {
  assert(addr + len - 1 < PORT_IO_SPACE_MAX);
  int mapid = find_mapid_by_addr(maps, nr_map, addr);
  assert(mapid != -1);
  map_write(addr, len, data, &maps[mapid]);
}
```

注意 `pio_read` 返回 `uint32_t`（不是 `word_t`），因为端口 I/O 在 x86 上最大 32 位；且这里 `assert(mapid != -1)`——访问未注册端口直接断言失败，不像 MMIO 那样经 `check_bound(NULL)` 走 panic 文案。

设备侧的 MMIO/PIO 切换以串口为例，见 [src/device/serial.c:43-51](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/serial.c#L43-L51)，timer/keyboard/vga/audio/disk 同构：

```c
void init_serial() {
  serial_base = new_space(8);
#ifdef CONFIG_HAS_PORT_IO
  add_pio_map ("serial", CONFIG_SERIAL_PORT, serial_base, 8, serial_io_handler);
#else
  add_mmio_map("serial", CONFIG_SERIAL_MMIO, serial_base, 8, serial_io_handler);
#endif
}
```

同一份设备代码，靠 `CONFIG_HAS_PORT_IO` 在 PIO 与 MMIO 间切换——这是 NEMU「一套设备代码适配多 ISA」的体现。

#### 4.4.4 代码实践

**实践目标**：对比 MMIO 与 PIO 两套映射的异同。

**操作步骤**：

1. 在 [src/device/io/mmio.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/mmio.c) 与 [src/device/io/port-io.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/port-io.c) 之间做逐行对照，列出：
   - 共同点：都用 `IOMap maps[NR_MAP]`、都用 `find_mapid_by_addr`、最终都调 `map_read`/`map_write`。
   - 差异点：MMIO 有 `report_mmio_overlap` 重叠检测、经 `fetch_mmio_map` 间接查找、由 `paddr_read/write` 驱动；PIO 无重叠检测、直接 `find_mapid_by_addr`、由 `pio_read/write` 驱动、地址是 16 位 `ioaddr_t`。
2. 在 [src/device/Kconfig:10-13](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/Kconfig#L10-L13) 确认 `HAS_PORT_IO` 仅 x86 默认开启，所以 riscv 下所有设备走 `add_mmio_map` 分支。

**需要观察的现象**：两套机制共用 `map.c` 的核心（`map_read`/`map_write`/`new_space`/`check_bound`），差异只在「注册时的校验」与「运行时的入口函数」。

**预期结果**：能填出下表——

| 维度 | MMIO | PIO |
|------|------|-----|
| 地址类型 | `paddr_t` | `ioaddr_t`(uint16_t) |
| 驱动入口 | `paddr_read/write` → `mmio_read/write` | `pio_read/write` |
| 重叠检测 | 有 | 无 |
| 适用 ISA | 全部 | 仅 x86 |
| 共用核心 | `map_read`/`map_write` | 同左 |

#### 4.4.5 小练习与答案

**练习 1**：为什么 `add_pio_map` 不需要像 `add_mmio_map` 那样检测与 pmem 重叠？

**答案**：PIO 使用独立的 16 位端口地址空间，与物理内存地址空间完全隔离，端口地址 `0x3f8` 等永远不会与 pmem 的 `0x80000000` 起段混淆，无需也无法做这种跨空间的重叠检查。

**练习 2**：`pio_read` 返回 `uint32_t` 而 `mmio_read` 返回 `word_t`，为什么类型不同？

**答案**：PIO 是 x86 专属，x86 的 `in` 指令最大读 32 位（`in eax, dx`），固定用 `uint32_t` 即可；MMIO 走内存总线，宽度随 ISA 变化（RV32 是 32 位、RV64 是 64 位），用 `word_t` 才能自适应 ISA 宽度（u4-l12 讲过的「宽度基因」）。

### 4.5 init_device 装配流程与 paddr 分流

#### 4.5.1 概念说明

前面三个模块讲清了「单条 IOMap 怎么注册、怎么查找、怎么读写」。本模块把它们串成一张完整的网：启动时谁调用谁把设备装起来，运行时一条访存指令怎么从 `paddr_write` 走到设备回调。

这是本讲的总收尾，也是综合实践的依据。

#### 4.5.2 核心流程

**启动期装配**（由 u1-l3 的 `init_monitor` 调用 `init_device`）：

```
init_device():
  IFDEF(CONFIG_TARGET_AM, ioe_init())   # AM 模式用 AM 的 IOE
  init_map()                            # 分配 32MB io_space 池
  init_serial()  → new_space + add_mmio_map/add_pio_map
  init_timer()   → 同上 + add_alarm_handle(timer_intr)
  init_vga() / init_i8042() / init_audio() / init_disk() / init_sdcard()
  IFNDEF(CONFIG_TARGET_AM, init_alarm())  # native 模式才装 SIGVTALRM 时钟
```

**运行期 MMIO 写路径**（以 CPU 往串口写一个字符为例）：

```
CPU 执行 sb 到 0xa00003f8
  → vaddr_write (u4-l13，当前透传)
  → paddr_write(addr, len, data)            # paddr.c:60
      in_pmem(addr)? 否（0xa00003f8 不在 [0x80000000, 0x8fffffff]）
      IFDEF(CONFIG_DEVICE, mmio_write(addr, len, data))   # paddr.c:62
        → fetch_mmio_map(addr)              # mmio.c:24
            → find_mapid_by_addr(...)        # map.h:37，命中 serial，difftest_skip_ref()
        → map_write(addr, len, data, &maps[i])   # map.c:64
            check_bound(...)
            host_write(serial_base+0, 1, data)   # 字符落进缓冲
            invoke_callback(serial_io_handler, 0, 1, true)  # serial.c:31
              → serial_putc(serial_base[0])      # 输出到 stderr
```

**运行期轮询**（由 u3-l9 的 `execute` 每步调用 `device_update`）：刷新 VGA、抽取 SDL 事件（键盘/退出），按 `TIMER_HZ` 节流。

#### 4.5.3 源码精读

`init_device` 见 [src/device/device.c:76-89](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L76-L89)：

```c
void init_device() {
  IFDEF(CONFIG_TARGET_AM, ioe_init());
  init_map();

  IFDEF(CONFIG_HAS_SERIAL, init_serial());
  IFDEF(CONFIG_HAS_TIMER, init_timer());
  IFDEF(CONFIG_HAS_VGA, init_vga());
  IFDEF(CONFIG_HAS_KEYBOARD, init_i8042());
  IFDEF(CONFIG_HAS_AUDIO, init_audio());
  IFDEF(CONFIG_HAS_DISK, init_disk());
  IFDEF(CONFIG_HAS_SDCARD, init_sdcard());

  IFNDEF(CONFIG_TARGET_AM, init_alarm());
}
```

顺序有依赖：`init_map()` 必须最先，因为它分配 `io_space` 池，后续所有 `init_xxx` 里的 `new_space` 都依赖它；`init_alarm()` 必须在设备都装好之后，因为它注册的 `timer_intr` 要能触发设备中断（u6-l20 详述）。每个 `init_xxx` 都被 `IFDEF(CONFIG_HAS_*, ...)` 包裹，关闭对应 Kconfig 选项即不编译进来。

每步轮询 `device_update` 见 [src/device/device.c:36-67](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L36-L67)：

```c
void device_update() {
  static uint64_t last = 0;
  uint64_t now = get_time();
  if (now - last < 1000000 / TIMER_HZ) {
    return;
  }
  last = now;

  IFDEF(CONFIG_HAS_VGA, vga_update_screen());
  #ifndef CONFIG_TARGET_AM
  SDL_Event event;
  while (SDL_PollEvent(&event)) {
    switch (event.type) {
      case SDL_QUIT: nemu_state.state = NEMU_QUIT; break;
      #ifdef CONFIG_HAS_KEYBOARD
      case SDL_KEYDOWN:
      case SDL_KEYUP: { send_key(event.key.keysym.scancode, event.key.type == SDL_KEYDOWN); break; }
      #endif
      default: break;
    }
  }
  #endif
}
```

它用 `now - last < 1000000 / TIMER_HZ` 做节流（单位微秒，`TIMER_HZ` 默认 1000，即每 1000us 才真正轮询一次），避免每条指令都刷新屏幕拖慢模拟。SDL 事件抽取也在此处——这是「宿主机键盘→客机」的入口。

物理总线的分流是 MMIO 路径的起点，`paddr_read`/`paddr_write` 见 [src/memory/paddr.c:53-64](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c#L53-L64)：

```c
word_t paddr_read(paddr_t addr, int len) {
  if (likely(in_pmem(addr))) return pmem_read(addr, len);
  IFDEF(CONFIG_DEVICE, return mmio_read(addr, len));
  out_of_bound(addr);
  return 0;
}

void paddr_write(paddr_t addr, int len, word_t data) {
  if (likely(in_pmem(addr))) { pmem_write(addr, len, data); return; }
  IFDEF(CONFIG_DEVICE, mmio_write(addr, len, data); return);
  out_of_bound(addr);
}
```

这就是 u4-l12 留下的「三分支路由」：`in_pmem` 命中走 pmem，否则若开启 `CONFIG_DEVICE` 走 mmio，否则越界 panic。`in_pmem` 用无符号减法判区间，见 [include/memory/paddr.h:30-32](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/paddr.h#L30-L32)：`return addr - CONFIG_MBASE < CONFIG_MSIZE;`。

设备源文件的编译与否由 [src/device/filelist.mk:16-26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/filelist.mk#L16-L26) 控制，`io/` 目录恒编译，各设备按 `CONFIG_HAS_*` 选取，`alarm.c` 在 AM 模式下被 blacklist（u6-l20 详述）。

#### 4.5.4 代码实践

**实践目标**：完整画出从 `paddr_write` 到设备回调的调用链。

**操作步骤**：

1. 选定一个具体场景：CPU 执行 `sb x1, 0(x0)` 往 `0xa00003f8`（serial MMIO 地址）写一个字符 `'A'`。
2. 从 [src/memory/paddr.c:60-64](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c#L60-L64) 的 `paddr_write` 出发，依次标注每一跳的文件与行号：
   - `paddr_write(0xa00003f8, 1, 'A')`：`in_pmem` 为假 → `mmio_write(0xa00003f8, 1, 'A')`。
   - [mmio.c:61-63](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/mmio.c#L61-L63) `mmio_write` → `fetch_mmio_map(0xa00003f8)`。
   - [mmio.c:24-27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/mmio.c#L24-L27) `fetch_mmio_map` → `find_mapid_by_addr` 命中 serial（下标 0），并 `difftest_skip_ref()`。
   - [map.c:64-70](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/map.c#L64-L70) `map_write`：`check_bound` → `host_write(serial_base+0, 1, 'A')` → `invoke_callback(serial_io_handler, 0, 1, true)`。
   - [serial.c:31-41](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/serial.c#L31-L41) `serial_io_handler`：`is_write` 且 `offset==0` → `serial_putc(serial_base[0])` → 输出 `'A'` 到 stderr。
3. 把这条链画成流程图，标注每一步所在的文件:行号。
4. 思考链路上的两个「设备副作用」隔离点：`find_mapid_by_addr` 里的 `difftest_skip_ref()`、`map_write` 里回调在 `host_write` 之后。

**需要观察的现象**：整条链路从「内存写」语义无缝转化为「设备输出」语义，中间没有任何特殊指令——CPU 完全不知道 `0xa00003f8` 是设备，它只是普通地写内存。

**预期结果**：得到一张含 5 个节点、标注了文件行号的调用链图，并能指出「读路径（如读 RTC）的回调时序与写路径相反」。

#### 4.5.5 小练习与答案

**练习 1**：`init_device` 里 `init_map()` 为什么必须排在所有 `init_xxx` 之前？

**答案**：`init_map` 负责分配 32MB 的 `io_space` 池并把 `p_space` 游标指向起点；所有 `init_xxx` 内部调 `new_space(size)` 都是从这个池切内存，依赖 `p_space` 已初始化。若调换顺序，`new_space` 会对 NULL 池操作而崩溃。

**练习 2**：若关闭 `CONFIG_DEVICE`（`make menuconfig` 里不选 Devices），CPU 往 `0xa00003f8` 写会发生什么？

**答案**：`paddr_write` 里 `IFDEF(CONFIG_DEVICE, ...)` 分支被编译期消除，地址不在 pmem 又无 mmio 分支，直接落到 `out_of_bound(addr)` → `panic`。即不开启设备支持时，所有设备地址访问都判越界终止。

## 5. 综合实践

**任务**：用一个最小程序验证「CPU 写串口 → 字符出现在 stderr」的完整链路，并解释为何 MMIO 区域不能与 pmem 重叠。

**步骤**：

1. 用 `make menuconfig` 选 riscv32 + System mode，开启 `Devices` → `Enable serial`（默认开），确认 `CONFIG_SERIAL_MMIO` 默认为 `0xa00003f8`。
2. `make` 编译后 `make run` 运行内置镜像。
3. 阅读 [src/device/serial.c:31-41](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/serial.c#L31-L41) 与 [src/device/io/map.c:64-70](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/map.c#L64-L70)，在纸上画出从 `paddr_write(0xa00003f8, 1, ch)` 到 `serial_putc(ch)` 的完整调用链（参考 4.5.4）。
4. 解释重叠问题（参考 4.3）：若 MMIO 区间与 pmem 重叠，`paddr_read/write` 的 `in_pmem` 优先分支会先命中 pmem，设备永远收不到访问，且数据会被错误地写进物理内存；若两个 MMIO 互相重叠，`find_mapid_by_addr` 线性扫描只会命中先注册者，后注册的设备永远收不到访问。`report_mmio_overlap` 在注册期就把这两类错误 panic 掉，避免运行期隐蔽故障。
5. （可选）若手头有一个往 `0xa00003f8` 写字符的 RISC-V 测试程序，观察 stderr 是否出现对应字符；若无，则标记为**待本地验证**。

**验收标准**：能口述完整调用链的每一跳及其文件行号；能用「路由二义」「线性扫描只命中第一个」两点解释重叠检测的必要性。

## 6. 本讲小结

- NEMU 用一个通用 `IOMap` 结构 `{name, low, high, space, callback}` 描述「地址区间→设备」的绑定，`find_mapid_by_addr` 线性扫描查找，命中即 `difftest_skip_ref()` 以避开设备副作用。
- `map_read`/`map_write` 采用「数据路径 + 控制路径」双路径设计：**读先回调后读缓冲**（让设备准备数据），**写先写缓冲后回调**（让设备反应写入值）——串口与 RTC 分别是这两种时序的样板。
- MMIO 设备经 `paddr_read/write` 的三分支路由分流到 `mmio_read/write`，挂在物理地址总线上；`add_mmio_map` 在注册期做与 pmem 及已有映射的重叠检测，重叠即 panic。
- PIO 是 x86 专属的独立 16 位端口地址空间，由 `pio_read/write` 驱动，`add_pio_map` 不做重叠检测；同一份设备代码靠 `CONFIG_HAS_PORT_IO` 在 PIO 与 MMIO 间切换。
- `init_device` 按依赖顺序装配：先 `init_map` 建池，再各 `init_xxx` 注册设备，最后 `init_alarm`；`device_update` 每步轮询刷新 VGA 与抽取 SDL 事件，按 `TIMER_HZ` 节流。
- 设备源码按 `CONFIG_HAS_*` 条件编译，关闭某设备即不编译其 `.c`，访问其地址会因无映射而 panic。

## 7. 下一步学习建议

- 下一讲 **u6-l19 典型外设实现** 将深入串口、定时器、键盘、VGA 四个设备的 `io_handler` 内部，本讲的 `serial_io_handler`/`rtc_io_handler` 是它们的入口，建议先吃透本讲的回调时序再读具体设备。
- **u6-l20 设备时钟与中断轮询** 会讲 `device_update` 的节流、`init_alarm` 的 SIGVTALRM 机制与 `dev_raise_intr` 中断挂起链路，承接本讲 `init_device` 末尾的 `init_alarm()`。
- 若对 `paddr_read/write` 的三分支路由还不够熟，可回看 u4-l12 物理内存 paddr 中 `in_pmem` 的无符号减法区间判定。
- 对差分测试感兴趣的可预读 u8-l24，理解本讲多次出现的 `difftest_skip_ref()` 为何是设备访问的必需配套。
