# TUN 工作线程：出站加密入口

## 1. 本讲目标

本讲精读 `src/wireguard/workers.rs` 中的 `tun_worker`，它是**出站方向（从本机发往对端）数据的第一个处理者**。学完本讲你应该能够：

- 说清一个明文 IP 包从 TUN 网卡被读出，到最终交给路由器加密发送管道的**完整步骤**。
- 手算 `padding()` 函数对任意 `(payload, mtu)` 输入的返回值，并解释「按 16 字节向上对齐」和「不超过 MTU」两条不变量。
- 解释 `msg` 缓冲区为什么要在头部预留 `SIZE_MESSAGE_PREFIX` 字节、在尾部预留 `CAPACITY_MESSAGE_POSTFIX` 字节，以及那个多余的 `+1` 是怎么回事。
- 说明设备处于 down 态（`mtu == 0`）时，`tun_worker` 为什么用 `continue` 而不是 `return`。

本讲承接 u3-l1 建立的「胶水层 + 三类工作线程」全局视角，只把目光聚焦在 `tun_worker` 这一条出站链路上，把 `udp_worker` / `handshake_worker` 以及路由器内部当成下一站的黑盒。

## 2. 前置知识

在进入源码前，先约定几个本讲反复用到的概念：

- **TUN 虚拟网卡**：一块用软件模拟的网卡，本机路由表把「去往 VPN 内网」的 IP 包投递到它。WireGuard 从 TUN **读**到的就是**明文 IP 包**（还没有任何密码学保护）。
- **MTU（最大传输单元）**：这块网卡单次能承载的最大 IP 包字节数。WireGuard 把设备的「启用/停用」也编码进 MTU——up 时 `mtu = 实际值`，down 时 `mtu = 0`（详见 4.1）。
- **传输报文（transport message）**：WireGuard 实际通过 UDP 发出去的密文报文，布局是 `传输头 ‖ 密文载荷 ‖ 认证标签`。传输头是 `TransportHeader`（16 字节），认证标签是 `SIZE_TAG`（16 字节）。
- **就地（in-place）构造**：不把数据拷来拷去，而是在同一块缓冲区里直接写入报文头、原地加密、追加标签。`msg` 缓冲区的「前缀 + 后缀」就是为这件事预留的。
- **OFFSET / 前缀**：`Tun::Reader::read(buf, offset)` 会把读到的 IP 包写进 `buf[offset..]`，把 `[0..offset]` 留空给报文头。这是 u2-l1 / u2-l2 已建立的零拷贝约定。
- **WaitCounter 与 worker 生命周期**（来自 u3-l1）：每个 TUN reader 都有一个 `tun_worker` 线程，被 `tun_readers` 计数；所有 `tun_worker` 退出时 `wg.wait()` 才会解除阻塞。因此 `tun_worker` 的「结束时机」很关键。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/wireguard/workers.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs) | 本讲主文件：`padding()`、`tun_worker`，以及同文件里的 `udp_worker` / `handshake_worker`（本讲只作对照）。 |
| [src/wireguard/router/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/mod.rs) | 路由器对外常量：`SIZE_TAG`、`SIZE_MESSAGE_PREFIX`、`CAPACITY_MESSAGE_POSTFIX`。 |
| [src/wireguard/router/messages.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/messages.rs) | `TransportHeader` 结构，决定了前缀有多大。 |
| [src/wireguard/router/device.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs) | `Device::send`——`tun_worker` 调用的下一站。 |
| [src/wireguard/router/peer.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs) | `Peer::send`——把包交给 `SendJob` 或暂存为 `staged_packets`。 |
| [src/wireguard/constants.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/constants.rs) | `MESSAGE_PADDING_MULTIPLE = 16`。 |
| [src/platform/tun.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs) | `Reader::read` 的 `offset` 契约。 |
| [src/wireguard/wireguard.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs) | `wg.mtu` 字段的 up/down 语义。 |

## 4. 核心概念与源码讲解

### 4.1 出站加密入口：tun_worker 整体流程

#### 4.1.1 概念说明

`tun_worker` 是一条**常驻、阻塞、每个 TUN reader 一条**的线程。它的职责非常单一：**从 TUN 读一个明文 IP 包 → 填充对齐 → 交给路由器加密发送**。它本身不做任何密码学运算，只是一个「搬运 + 整形」的入口。

之所以要为每个 reader 单独开一条线程，是因为 TUN 的 `read` 是阻塞调用——一个 reader 只能串行地一个个读包，开多条 reader（`PlatformTun::create` 返回的是一个 reader 列表）并各配一条 worker，就能并行地把多个 IP 包喂进加密管道，提升吞吐。

#### 4.1.2 核心流程

`tun_worker` 是一个无限循环，每一轮处理一个 IP 包，步骤如下：

```text
loop {
  1. 读取当前 MTU（原子加载，每轮重新读）
  2. 按 MTU 分配一个足够大的 msg 缓冲区（前缀 + 载荷 + 1 + 后缀）
  3. reader.read(msg, SIZE_MESSAGE_PREFIX)  ← 阻塞等下一个 IP 包，写入 msg[16..]
     └─ 读失败 → break，结束本线程
  4. 若 mtu == 0（设备 down）→ continue，丢弃这个包但保持线程存活
  5. padded = padding(payload, mtu)        ← 按 16 字节向上对齐，且 ≤ MTU
  6. msg.truncate(SIZE_MESSAGE_PREFIX + padded)
  7. wg.router.send(msg)                   ← 交给路由器加密发送管道
}
```

注意第 1 步：MTU 在**循环顶部、每轮重新加载**。这是后面第 4 步 `mtu == 0` 能在设备重新 up 后自动恢复的关键。

#### 4.1.3 源码精读

先看整个函数骨架，对应 [src/wireguard/workers.rs:57-101](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L57-L101)：

```rust
pub fn tun_worker<T: Tun, B: UDP>(wg: &WireGuard<T, B>, reader: T::Reader) {
    loop {
        // create vector big enough for any transport message (based on MTU)
        let mtu = wg.mtu.load(Ordering::Relaxed);
        let size = mtu + SIZE_MESSAGE_PREFIX + 1;
        let mut msg: Vec<u8> = vec![0; size + CAPACITY_MESSAGE_POSTFIX];

        // read a new IP packet
        let payload = match reader.read(&mut msg[..], SIZE_MESSAGE_PREFIX) {
            Ok(payload) => payload,
            Err(e) => { debug!("TUN worker, failed to read from tun device: {}", e); break; }
        };
        ...
        if mtu == 0 { continue; }
        ...
        let e = wg.router.send(msg);
        debug!("TUN worker, router returned {:?}", e);
    }
}
```

三个关键点：

1. **每轮重新加载 MTU**——[workers.rs:60](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L60) 的 `wg.mtu.load(Ordering::Relaxed)`。`wg.mtu` 是一个 `AtomicUsize`（见 [wireguard.rs:46](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L46)）。`up(mtu)` 把它设成真实 MTU（[wireguard.rs:153-158](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L153-L158)），`down()` 把它清成 0（[wireguard.rs:127-137](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L127-L137)），初始值也是 0。所以 `mtu == 0` 就是「设备当前处于 down 态」的统一信号。

2. **`reader.read` 的 offset 契约**——[workers.rs:65](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L65) 传入 `SIZE_MESSAGE_PREFIX` 作为 offset。看 [platform/tun.rs:31-49](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs#L31-L49) 的契约：IP 包被写进 `buf[offset..]`，`[0..offset]` 留空。文档原话是「This space is later used to construct the transport message inplace」——这块前缀正是为稍后**就地写入传输头**预留的。

3. **读失败即 `break`**——[workers.rs:67-70](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L67-L70)。TUN reader 关闭（例如设备被销毁）时 `read` 返回错误，worker 跳出循环、线程结束，`WaitCounter` 计数减一。这是 `wg.wait()` 能够正常返回的退出路径。

#### 4.1.4 代码实践：观察缓冲区布局

**实践目标**：在不改源码的前提下，手算一次缓冲区分配，确认「前缀 + 载荷 + 后缀」能装下最终密文。

**操作步骤**：

1. 假设 `MTU = 1420`。读 [router/mod.rs:26-28](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/mod.rs#L26-L28)，确认三个常量：`SIZE_TAG = 16`、`SIZE_MESSAGE_PREFIX = size_of::<TransportHeader>() = 16`、`CAPACITY_MESSAGE_POSTFIX = SIZE_TAG = 16`。（`TransportHeader` 是 `u32 + u32 + u64` = 16 字节，见 [router/messages.rs:7-13](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/messages.rs#L7-L13)。）
2. 代入 [workers.rs:61-62](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L61-L62)：
   - `size = 1420 + 16 + 1 = 1437`
   - `msg.len() = 1437 + 16 = 1453`
3. 想象读到一个 100 字节的 IP 包（`payload = 100`），它占据 `msg[16..116)`。

**需要观察的现象**：最终加密后的传输报文 = 传输头 16 + 密文（= padded 载荷，见 4.2）+ 标签 16。即使 padded 取到最大值 1420，总长也才 `16 + 1420 + 16 = 1452`，比 1453 小 1。

**预期结果**：缓冲区恰好能容纳「满 MTU 载荷 + 头 + 标签」，并且多出 1 字节余量。那个 `+1`（[workers.rs:61](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L61)）就是这 1 字节的安全 slack，确保即便 `read` 写满 MTU 字节、随后就地追加 16 字节标签也不会越界。

> 说明：本实践为源码阅读型实践，不修改源码、不实际运行；数值可按上面公式自行复核。**待本地验证**：若你想看到真实日志，可在 `RUST_LOG=wireguard_rs=trace` 下运行，观察 `TUN worker, IP packet of {} bytes (MTU = {})` 这条 [debug 行](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L72)。

#### 4.1.5 小练习与答案

**练习 1**：`tun_worker` 为什么把 MTU 的加载放在循环体最开头，而不是循环外只读一次？

**参考答案**：因为 MTU 会随 `up` / `down` 动态变化。放在循环外只会在首次进入 worker 时读到一个固定值（初始还是 0）。放在循环内每轮重读，既能感知 `down`（变 0，触发第 4 步丢弃），也能在重新 `up` 后立刻拿到新的 MTU 重新分配正确大小的缓冲区。

**练习 2**：`reader.read` 返回错误时 `break`，而 `mtu == 0` 时 `continue`。这两种处理方式的本质区别是什么？

**参考答案**：`break` = 「reader 没了，线程没有继续存在的意义」，是**永久退出**，会让 `WaitCounter` 减计数，推动 `wg.wait()` 返回；`continue` = 「reader 还在，只是设备暂时 down 了」，是**保留线程、丢弃当前包、等下一轮」，这样设备重新 up 后无需重启线程即可恢复转发。

---

### 4.2 padding()：载荷对齐与 MTU 约束

#### 4.2.1 概念说明

`padding()` 是一个 `const fn`，它回答一个问题：**给定的 IP 包载荷，在加上传输头和标签之前，应该被填充到多少字节？** 它要同时满足两条硬约束：

- **向上对齐到 16 字节的整数倍**：WireGuard 把传输报文的密文载荷填充到 `MESSAGE_PADDING_MULTIPLE`（16）的整数倍（见 [constants.rs:31-33](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/constants.rs#L31-L33)）。这样做一方面把可观测的报文长度「取整」，降低基于精确长度的流量分析精度；另一方面让载荷与 AEAD（ChaCha20-Poly1305）的处理块对齐。
- **绝不超过 MTU**：填充后的总密文还得能塞进 UDP 报文、能通过物理网卡。当「向上取整」会越过 MTU 时，直接截到 MTU。

#### 4.2.2 核心流程

函数本身只有三行算术，定义在 [src/wireguard/workers.rs:46-55](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L46-L55)：

```rust
#[inline(always)]
const fn padding(size: usize, mtu: usize) -> usize {
    const fn min(a: usize, b: usize) -> usize {
        let m = (a < b) as usize;
        a * m + (1 - m) * b
    }
    let pad = MESSAGE_PADDING_MULTIPLE;          // 16
    min(mtu, size + (pad - size % pad) % pad)
}
```

之所以内嵌一个 `const fn min`（[workers.rs:48-52](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L48-L52)），是因为 `const fn` 里早期 Rust 不能调用标准库的 `usize::min`，只能用「无分支」的算术技巧手写一个：`m = (a < b) as usize`（真为 1 假为 0），返回 `a*m + (1-m)*b`，等价于 `if a < b { a } else { b }` 但不依赖 `if`。

设 \(p = 16\)（`MESSAGE_PADDING_MULTIPLE`），\(s\) 为载荷字节数，\(m\) 为 MTU。令余数 \(r = s \bmod p\)。核心表达式是：

\[
\text{pad\_amount} = (p - r) \bmod p
\]

- 当 \(r = 0\)（已是 16 的倍数）：\((16 - 0) \bmod 16 = 16 \bmod 16 = 0\)，不加任何填充。
- 当 \(r > 0\)：\((16 - r)\) 落在 \([1, 15]\)，再模 16 不变，即补到下一个 16 的倍数。

于是「向上取整后的长度」\(= s + \text{pad\_amount} = \lceil s / 16 \rceil \times 16\)，最后再与 MTU 取 min：

\[
\boxed{\;\text{padding}(s, m) = \min\!\big(m,\; \lceil s / 16 \rceil \times 16\big)\;}
\]

由此推出两条不变量（也正是函数返回后被 `debug_assert!` 校验的，见 [workers.rs:87-95](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L87-L95)）：

1. \(\text{padding}(s, m) \le m\)（恒不超过 MTU）。
2. 返回值要么 **等于 MTU**，要么 **是 16 的整数倍**（两者必居其一）。

举几个具体例子（`MTU = 1420`）：

| 载荷 \(s\) | \(s \bmod 16\) | 向上取整 | \(\min(1420, \cdot)\) | 说明 |
|---|---|---|---|---|
| 0   | 0  | 0    | **0**    | 空载荷（keepalive 走的是另一条路，见 4.3） |
| 16  | 0  | 16   | **16**   | 已对齐，不变 |
| 20  | 4  | 32   | **32**   | 补 12 字节 |
| 40  | 8  | 48   | **48**   | 补 8 字节 |
| 100 | 4  | 112  | **112**  | 补 12 字节 |
| 1408| 0  | 1408 | **1408** | 最大的「干净」倍数 |
| 1410| 2  | 1424 | **1420** | 取整会超 MTU，截到 MTU（此时非 16 倍数，但 == MTU，合规）|
| 1420| 12 | 1424 | **1420** | 同上 |

最后一行正是「不变量 2」的典型场景：返回值 == MTU，即使它不是 16 的倍数也合法。源码里对应的断言写得很巧——[workers.rs:88-95](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L88-L95)：

```rust
debug_assert!(padded <= mtu);
debug_assert_eq!(
    if padded < mtu { (msg.len() - SIZE_MESSAGE_PREFIX) % MESSAGE_PADDING_MULTIPLE } else { 0 },
    0
);
```

`msg` 此刻已被 `truncate(SIZE_MESSAGE_PREFIX + padded)`（[workers.rs:86](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L86)），所以 `msg.len() - SIZE_MESSAGE_PREFIX` 正好等于 `padded`。于是断言读作：「若 padded 严格小于 MTU，则它必须是 16 的倍数；若 padded == MTU，则跳过模检查」。这就是不变量 2 的可执行形态。

#### 4.2.3 源码精读

把调用点串起来看（都在 `tun_worker` 内）：

- 读到载荷长度 `payload`（[workers.rs:65](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L65) 的返回值）。
- 计算 `padded = padding(payload, mtu)`（[workers.rs:80](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L80)）。
- 把 `msg` 截断为 `SIZE_MESSAGE_PREFIX + padded`（[workers.rs:86](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L86)）：即「16 字节传输头占位 + padded 字节载荷」。

注意：截断是**缩短**，不会清零 `[16, 16+padded)` 之外的旧字节——但截断后 `Vec` 的逻辑长度变了，下游加密只会处理 `msg[16..16+padded]`。被「填充」进来的那部分（`payload..padded`）沿用 `vec![0; ...]` 初始化的 0，这就是填充字节的内容（全零）。配合后续就地加密，对端解密后看到的就是 `真实载荷 ‖ 零填充`，再由 IP 头里的「总长度」字段截取真实部分即可。

#### 4.2.4 代码实践：为 padding() 编写断言测试

**实践目标**：用一组 `(payload, mtu)` 组合验证 `padding()` 满足「对齐且 ≤ MTU」两条不变量，并补充对 `mtu == 0` 时 `tun_worker` 行为的说明。

**操作步骤**：

1. `padding` 是 `workers` 模块内的**私有** `const fn`（[workers.rs:47](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L47) 没有 `pub`）。要直接测它，最干净的做法是在 `src/wireguard/workers.rs` 文件末尾加一个 `#[cfg(test)]` 子模块——子模块可以访问父模块的私有项。

2. 加入下面这段**示例代码**（非项目原有代码，仅供练习；放入 `workers.rs` 末尾即可随 `cargo test` 编译运行）：

   ```rust
   // 示例代码：添加到 src/wireguard/workers.rs 末尾
   #[cfg(test)]
   mod padding_tests {
       use super::padding;                 // 访问父模块的私有 const fn
       use super::MESSAGE_PADDING_MULTIPLE; // 由 workers.rs 顶部 use 引入，子模块可见

       #[test]
       fn test_padding_invariants() {
           for &(s, mtu) in &[
               (0usize, 1420usize), (16usize, 1420usize), (20usize, 1420usize),
               (40usize, 1420usize), (100usize, 1420usize), (1408usize, 1420usize),
               (1410usize, 1420usize), (1420usize, 1420usize), (1500usize, 1500usize),
           ] {
               let padded = padding(s, mtu);
               // 不变量 1：绝不超过 MTU
               assert!(padded <= mtu, "padded={padded} > mtu={mtu}");
               // 不变量 2：要么 == MTU，要么是 16 的整数倍
               assert!(
                   padded == mtu || padded % MESSAGE_PADDING_MULTIPLE == 0,
                   "padded={padded} 既不等于 mtu={mtu} 也不是 16 的倍数"
               );
           }
       }

       #[test]
       fn test_padding_known_values() {
           assert_eq!(padding(0, 1420), 0);      // 空载荷
           assert_eq!(padding(20, 1420), 32);    // 20 -> 上取整到 32
           assert_eq!(padding(1500, 1500), 1500);// 取整会超 MTU -> 截到 MTU
       }
   }
   ```

3. 运行 `cargo test padding`。

**需要观察的现象**：所有断言通过；特别确认 `padding(1500, 1500) == 1500`（不是 1504）这一「截到 MTU」的边界。

**预期结果**：`test_padding_invariants` 和 `test_padding_known_values` 均通过，证明两条不变量成立。

4. **补充说明：`mtu == 0` 时为何 `continue`**。结合 [workers.rs:74-77](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L74-L77)：`mtu == 0` 表示设备处于 down 态（见 4.1）。此时若用 `return`，该 `tun_worker` 线程会**永久退出**，即便设备随后重新 `up`，这条 reader 也不再有人消费，对应流量会静默丢失；而且会让 `tun_readers` / `WaitCounter` 错误地以为「该 worker 正常结束」，干扰 `wg.wait()` 的生命周期判断。改用 `continue` 则只是**丢弃当前这一个包**（设备都 down 了，本就不该转发），线程在下一轮循环顶部重新加载 MTU，一旦设备 up 起来就自动恢复转发。注意：即便 down，`reader.read` 仍可能返回一个包（队列里残留或对端仍在发），所以这个 `continue` 还承担了「down 期间持续排空/丢弃」的职责。

> 如果你不想改动源码，也可以把 `padding` 的纯算术体原样复制到独立测试文件里测——它不依赖任何外部状态，复制版与原版行为一致。**待本地验证**：实际 `cargo test` 是否通过。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `padding` 里要先算 `(pad - size % pad) % pad`，而不是简单地 `size % pad == 0 ? 0 : pad - size % pad`？

**参考答案**：因为这是 `const fn`，写作者选择了**无 `if` 的纯算术表达**来兼容 `const` 求值。`(pad - size % pad) % pad` 一行同时覆盖了两种情况：`size % pad == 0` 时 `(16 - 0) % 16 = 0`，否则补到下一个倍数。等价但无需分支。

**练习 2**：当 `payload = 1500`、`MTU = 1500` 时，`padded = 1500`。这个值不是 16 的倍数，为什么仍然合法、不会触发 `debug_assert`？

**参考答案**：因为当 `padded == mtu` 时，[workers.rs:88-95](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L88-L95) 的断言走 `else { 0 }` 分支，直接得 `0 == 0`。不变量的准确表述是「要么等于 MTU，要么是 16 的倍数」，而非「必须是 16 的倍数」。

---

### 4.3 wg.router.send：进入加密发送管道

#### 4.3.1 概念说明

`tun_worker` 最后一行 `wg.router.send(msg)` 把整形好的缓冲区交给**路由器**。注意：路由器虽然名字里有「路由」，但它本质是 WireGuard 的**数据面（packet protector）**，负责「按 cryptokey 规则选 peer → 加密 → 排序 → 发到 UDP」。`tun_worker` 只负责把包送进这个管道的入口，之后包要么被立刻加密发出，要么因为没有可用密钥而被**暂存**。

这里要建立一个关键认知：**没有密钥时包不会丢**。握手尚未完成、或密钥已过期时，包会被收进该 peer 的 `staged_packets` 队列，等新密钥就位后再批量补发。这解释了为什么 WireGuard 在握手期间不会丢用户数据。

#### 4.3.2 核心流程

调用链是三跳：

```text
tun_worker
  └─ wg.router.send(msg)                 ← router/device.rs: Device::send
       ├─ table.get_route(packet)         ← 按 IP 目的地址最长前缀匹配选 peer
       │    └─ 找不到 → 返回 Err(NoCryptoKeyRoute)，包被丢弃
       └─ peer.send(msg, true)            ← router/peer.rs: Peer::send（stage=true）
            ├─ 无加密密钥 / nonce 用尽 → push 进 staged_packets，回调 need_key
            └─ 有密钥 → 构造 SendJob，入 outbound 有序队列，再投到并行工作队列加密
```

注意 `send` 的第二个参数 `stage = true`——它表示「若现在没密钥，把这个包暂存起来」，而不是直接丢。

#### 4.3.3 源码精读

第一跳，`Device::send`，[src/wireguard/router/device.rs:181-201](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L181-L201)：

```rust
pub fn send(&self, msg: Vec<u8>) -> Result<(), RouterError> {
    debug_assert!(msg.len() > SIZE_MESSAGE_PREFIX);
    // ignore header prefix (for in-place transport message construction)
    let packet = &msg[SIZE_MESSAGE_PREFIX..];     // 跳过前缀，拿到纯 IP 包
    // lookup peer based on IP packet destination address
    let peer = self.state.table.get_route(packet).ok_or(RouterError::NoCryptoKeyRoute)?;
    // schedule for encryption and transmission to peer
    peer.send(msg, true);
    Ok(())
}
```

- [device.rs:189](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L189) 把前缀「切掉」拿到纯 IP 包——只在**视图上**切（`&msg[16..]`），底层内存仍是同一块，这就是「就地」的体现。
- [device.rs:192-196](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L192-L196) 用 IP 包的目的地址在 cryptokey 路由表里查 peer。查不到就返回 `NoCryptoKeyRoute`——这个错误最终会冒到 `tun_worker` 的 `let e = wg.router.send(msg);`（[workers.rs:98](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L98)），但 `tun_worker` 只是 `debug!` 打一条日志，不重试、不阻塞下一个包。
- [device.rs:199](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L199) 把**整个 `msg`**（含前缀）传给 `peer.send`，第二参数 `true` 即「允许暂存」。

第二跳，`Peer::send`，[src/wireguard/router/peer.rs:252-298](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L252-L298)，核心是三种分支：

```rust
pub(super) fn send(&self, msg: Vec<u8>, stage: bool) {
    let (job, need_key) = {
        let mut enc_key = self.enc_key.lock();
        match enc_key.as_mut() {
            None => { /* 无密钥 */ if stage { self.staged_packets.lock().push_back(msg); } (None, true) }
            Some(state) => {
                if state.nonce >= REJECT_AFTER_MESSAGES - 1 {
                    /* 密钥过期 */ *enc_key = None; if stage { push_back(msg); } (None, true)
                } else {
                    let job = SendJob::new(msg, state.nonce, state.keypair.clone(), self.clone());
                    if self.outbound.push(job.clone()) { state.nonce += 1; (Some(job), false) }
                    else { (None, false) }  // 有序队列满，丢弃（背压）
                }
            }
        }
    };
    if need_key { C::need_key(&self.opaque); }          // 触发「需要新密钥」回调 → 发起握手
    if let Some(job) = job { self.device.work.send(JobUnion::Outbound(job)); } // 投到并行加密队列
}
```

三个要点：

- **暂存而非丢弃**——[peer.rs:259-261](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L259-L261)（无密钥）与 [peer.rs:266-272](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L266-L272)（nonce 耗尽，`REJECT_AFTER_MESSAGES`）两处都把 `msg` push 进 `staged_packets`，前提正是 `stage == true`。tun_worker 路径恒为 true，所以用户数据不会因「正在握手」而丢。
- **触发握手**——只要进了暂存分支，`need_key = true`，于是在 [peer.rs:288-292](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L288-L292) 调用 `C::need_key(&self.opaque)`。这个回调会通知上层（`PeerInner` 的 `Callbacks` 实现，详见 u7-l1）发起一次新握手。握手完成后新密钥就位，`staged_packets` 里的包会在 `send_staged`（[peer.rs:301-314](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L301-L314)）里被补发。
- **入有序队列再并行加密**——[peer.rs:275-279](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L275-L279) 为这个包分配一个 `nonce`（每包递增），构造 `SendJob` 并 push 进 `outbound` 这个**有序队列**；随后 [peer.rs:294-297](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L294-L297) 把 job 投到并行工作队列。`SendJob` 内部会用 `ring` 的 ChaCha20-Poly1305 对 `msg[16..16+padded]` 就地加密、把传输头写进 `msg[0..16]`、再追加 16 字节标签（这部分细节留给 u5-l2）。

> keepalive 的旁路：`PeerHandle::send_keepalive`（[peer.rs:500-503](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L500-L503)）发的是 `vec![0u8; SIZE_MESSAGE_PREFIX]`——一个**只有前缀、载荷为 0** 的包，它不经过 `tun_worker`，但同样走 `Peer::send`。这呼应了 4.2 表格里 `padding(0, mtu) == 0` 的那一行：keepalive 的 padded 载荷就是 0。

#### 4.3.4 代码实践：跟踪一个包的三种命运

**实践目标**：用文字（不运行）跟踪同一个 IP 包在「有密钥 / 无密钥 / 无路由」三种情况下的不同归宿。

**操作步骤**：阅读 [device.rs:181-201](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L181-L201) 与 [peer.rs:252-298](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L252-L298)，填写下表。

| 情况 | `get_route` 结果 | 进入 `Peer::send` 后走向 | 最终归宿 | 是否丢包 |
|------|------------------|--------------------------|----------|----------|
| ① 有密钥 | 命中 peer | `SendJob` → outbound 队列 → 并行加密 → UDP 发出 | 对端收到密文 | 否 |
| ② 无密钥（握手未完成/已过期） | 命中 peer | `staged_packets.push_back` + `need_key` 回调 | 暂存，握手完成后由 `send_staged` 补发 | 否（暂存） |
| ③ 无 cryptokey 路由 | `None` | 不进入 `Peer::send` | `tun_worker` 收到 `Err(NoCryptoKeyRoute)`，仅打 debug 日志 | 是（静默丢弃） |

**需要观察的现象 / 预期结果**：能清楚指出「②不会丢包」是 `stage = true` 与 `staged_packets` 共同保证的；而「③会丢包」是 WireGuard 的设计——没有匹配的 allowed-ip 就不转发，符合 cryptokey 路由语义。

#### 4.3.5 小练习与答案

**练习 1**：`Device::send` 里 `debug_assert!(msg.len() > SIZE_MESSAGE_PREFIX)`（[device.rs:182](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L182)）在防什么？

**参考答案**：它要求传入的 `msg` 除了 16 字节前缀之外**至少还有 1 字节载荷**。因为后面 `let packet = &msg[SIZE_MESSAGE_PREFIX..]` 要把这个载荷当 IP 包去查路由——载荷为空（纯 keepalive）根本不是 IP 包，没法 `get_route`。纯 keepalive 走的是 `send_keepalive` 旁路，不会进 `Device::send`，所以这条断言排除了「空载荷误入主路径」的编程错误。

**练习 2**：`Peer::send` 第二参数叫 `stage`。`tun_worker` 路径传 `true`，而 `send_staged` 里补发时传 `false`（[peer.rs:309](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L309)）。为什么补发时不能再用 `true`？

**参考答案**：补发发生在 `send_staged` 内部，此时正在把暂存队列里的包逐个取出重发。如果补发时又遇到「仍无密钥」并把 `stage` 设为 true，就会把这些包**再次 push 回 `staged_packets`**，形成无限循环（包永远在队列里打转）。补发场景下若仍无密钥，正确的做法是直接放弃这一包（`stage = false` 不入队），避免循环。tun_worker 是「新包首次进入」，允许暂存；补发是「重试」，不能再暂存。

## 5. 综合实践

把本讲三节串起来，做一次完整的「纸面推演」。

**任务背景**：MTU = 1420，从 TUN 读到一个 **100 字节的 IPv4 包**，且对端 peer 已有可用加密密钥。

请完成：

1. **缓冲区布局**：写出 `msg` 初始分配长度（`vec![0; size + CAPACITY_MESSAGE_POSTFIX]`）。  
   *参考*：`size = 1420 + 16 + 1 = 1437`，`msg.len() = 1437 + 16 = 1453`。
2. **读入与截断**：`reader.read` 把 100 字节写入 `msg[16..116)`。计算 `padding(100, 1420)`，并写出 `msg.truncate(...)` 后的长度。  
   *参考*：`padding(100, 1420) = 112`（100 补 12 到 112）；截断后 `msg.len() = 16 + 112 = 128`。
3. **交给路由器**：`wg.router.send(msg)` 中 `packet = &msg[16..128]`（112 字节，含真实 100 字节 + 12 字节零填充），`get_route` 用这个 IP 包的目的地址选 peer。
4. **加密后体积**：`SendJob` 就地加密后，传输报文 = 传输头 16 + 密文 112 + 标签 16 = **144 字节**，经 UDP 发出。验证：`message_data_len(payload)`（[router/mod.rs:30-32](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/mod.rs#L30-L32)）= `payload + SIZE_MESSAGE_PREFIX + SIZE_TAG`，代入 padded 载荷 112 得 `112 + 16 + 16 = 144`，吻合。
5. **变式提问**：若此刻对端**没有可用密钥**，第 4 步会发生什么？请用一句话回答。  
   *参考*：包被 push 进 `staged_packets`（因 `stage = true`），并触发 `need_key` 回调发起握手；握手完成后由 `send_staged` 补发，包不丢失。
6. **变式提问**：若此刻设备处于 down 态（`mtu == 0`），这个包会怎样？  
   *参考*：`mtu == 0` 命中 [workers.rs:75-77](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L75-L77) 的 `continue`，包被丢弃、线程保留，等设备 up 后恢复。

## 6. 本讲小结

- `tun_worker` 是**出站方向**的入口线程，每条 TUN reader 一条；职责是「读 IP 包 → 填充对齐 → 交给路由器」，自身不做密码学。
- 缓冲区按 `mtu + SIZE_MESSAGE_PREFIX(16) + 1 + CAPACITY_MESSAGE_POSTFIX(16)` 分配，前缀用于就地写传输头，后缀留给 16 字节 AEAD 标签，`+1` 是安全 slack。
- `reader.read(buf, SIZE_MESSAGE_PREFIX)` 把 IP 包写进 `buf[16..]`，前缀留空——这是 `Tun::Reader::read` 契约（u2-l1/u2-l2）在数据面的落地。
- `padding(s, m) = min(m, ⌈s/16⌉ × 16)`：载荷向上对齐到 16 字节、且绝不超过 MTU；返回值「要么等于 MTU，要么是 16 的倍数」。
- `wg.router.send` → `table.get_route`（按目的 IP 选 peer）→ `peer.send(msg, true)`；**无密钥时包进 `staged_packets` 暂存并触发 `need_key` 握手，不丢包**。
- `mtu == 0`（down 态）用 `continue` 丢弃当前包但保留线程；`read` 出错才 `break` 永久退出，这是 `WaitCounter` 生命周期的正确配合。

## 7. 下一步学习建议

本讲把出站链路追到了 `wg.router.send` 这扇门。门后有三条值得继续深入的路：

- **加密细节**：`SendJob` 如何用 `ring::aead` 的 ChaCha20-Poly1305 就地加密、构造 nonce、写 `TransportHeader`——见 u5-l2「发送管道：加密（SendJob）」。
- **入站对照**：UDP 侧的 `udp_worker` 如何把报文按类型分流到握手或路由器——见 u3-l3「UDP 工作线程：入站消息分用」。建议读完 u3-l3 再回头看本讲，能形成「出入站成对」的完整图景。
- **有序与并行**：`outbound` 这个 `Queue` 如何做到「多线程并行加密、单线程按序发送」——见 u5-l4「有序队列 Queue」。

如果对「密钥从何而来、何时轮转」感兴趣，可跳读 u5-l6「密钥轮转 KeyWheel 与 Peer 生命周期」，它会解释 `staged_packets` 补发的触发时机。
