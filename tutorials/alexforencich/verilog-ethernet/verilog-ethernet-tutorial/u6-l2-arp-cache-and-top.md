# ARP 缓存与顶层 arp 模块

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `arp_cache` 的存储结构：一张「直接映射（direct-mapped）哈希表」，并能解释它的索引来源、冲突处理与替换策略。
- 读懂 `arp` 顶层模块如何把 `arp_eth_rx`、`arp_eth_tx`、`arp_cache` 三个子模块「布线」成一个完整的 ARP 服务：被动学习、IP→MAC 查询、未命中后自动发 ARP 请求、重试与超时。
- 理解 `CACHE_ADDR_WIDTH`、`REQUEST_RETRY_COUNT`、`REQUEST_RETRY_INTERVAL`、`REQUEST_TIMEOUT` 四个参数的物理含义与默认值。
- 重要的一个纠偏：本库的 `arp_cache` **不是 LRU**（尽管学习手册大纲里写了「LRU 淘汰」），它没有任何使用计数或时间戳，替换策略就是「直接映射覆写」。这一点我们会用源码与官方 testbench 一起证实。

## 2. 前置知识

本讲承接 **u6-l1（ARP 帧接收与发送）**，默认你已经掌握：

- **ARP 报文结构**：28 字节载荷，`HTYPE/PTYPE/HLEN/PLEN/OPER/SHA/SPA/THA/TPA`，请求 `OPER=1`、应答 `OPER=2`，并知道 `arp_eth_rx` 把载荷流拆成并行字段、`arp_eth_tx` 反向组装。
- **AXI-Stream 与「并行头 + 载荷流」的接口风格**（见 u1-l3、u3-l1）：`hdr_valid/hdr_ready` 握手头部，`tvalid/tready` 握手载荷。
- **lfsr/CRC**（见 u2-l1）：本讲的哈希函数就是复用 `rtl/lfsr.v` 算的 CRC-32。
- 一点网络常识：IP 子网掩码、网关的作用，以及为什么「跨网段通信要先解析网关的 MAC」。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [rtl/arp_cache.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_cache.v) | 直接映射哈希缓存：查询（IP→MAC）与写入两套独立接口，加 `clear_cache` 清空。 |
| [rtl/arp.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp.v) | 顶层「布线层」：例化收/发帧子模块 + 缓存，用一段组合状态机串联「查询→未命中→自动请求→重试→超时」。 |
| rtl/arp_eth_rx.v / rtl/arp_eth_tx.v | u6-l1 已精读，本讲只把它们当黑盒用。 |
| rtl/lfsr.v | u2-l1 已精读，被 `arp_cache` 用作哈希函数。 |
| tb/arp_cache/、tb/arp/ | 官方 cocotb testbench，本讲代码实践的依据。 |

---

## 4. 核心概念与源码讲解

### 4.1 哈希 ARP 缓存（arp_cache）

#### 4.1.1 概念说明

为什么需要缓存？ARP 的本质是「用广播问一句『这个 IP 是谁？』，被问到的设备单播回答『是我，我的 MAC 是 X』」。如果每发一个 IP 包都广播一次 ARP，网络上会充满 ARP 噪声。所以 MAC 解析的结果应当**缓存**起来反复用。

`arp_cache` 就是这张表，但它不是一张「能装下全网所有 IP」的大表，而是一张**固定深度、由哈希函数索引**的小表。核心设计取舍是：

- **直接映射（direct-mapped）**：每个 IP 经过哈希后**唯一地**映射到一个槽位（slot）。即 `index = hash(IP) mod 表大小`。一个槽位只能存一条记录。
- **存全 IP 做冲突判别**：因为不同 IP 可能哈希到同一个槽位（冲突），每个槽除了存 MAC，还存了完整的 32 位 IP。查询时不仅要槽有效，还要「槽里的 IP == 查询 IP」才算命中。
- **替换策略 = 直接覆写**：当写入一个新 IP 时，它直接写进 `hash(IP)` 指向的槽，**无条件覆盖**原来的内容。**没有 LRU、没有计数器、没有「最近最少使用」的概念**——这是本讲最容易被大纲误导的一点，源码里确实没有。代价是：若两个常用 IP 恰好哈希冲突，它们会互相把对方踢出缓存（thrashing）；好处是硬件极简，无需维护替换状态。

> 为什么用 CRC-32 当哈希？CRC-32 对 32 位输入会把它「充分打散」成 32 位输出（u2-l1 已说明它是 GF(2) 上的线性映射），取其低位作为表索引，能让相邻 IP（如 `192.168.1.11` 和 `192.168.1.12`）尽量分散到不同槽，降低冲突概率。

#### 4.1.2 核心流程

缓存有三条互相独立的「流水线」：**查询**、**写入**、**清空**。

```
查询 (2 拍流水线):
  拍0: query_request_valid & ready
       → 锁存 query_ip，rd_ptr ← hash(query_ip) 的低 CACHE_ADDR_WIDTH 位
  拍1: 读 valid_mem[rd_ptr] / ip_addr_mem[rd_ptr] / mac_addr_mem[rd_ptr]
       → 比较 ip_addr_mem[rd_ptr] == query_ip ?
       → 输出 query_response_valid + error(0=命中/1=未命中) + mac

写入 (2 拍流水线):
  拍0: write_request_valid & ready
       → 锁存 write_ip / write_mac，wr_ptr ← hash(write_ip) 的低位
  拍1: 把 {valid=1, ip, mac} 写进 [wr_ptr]

清空 clear_cache (占 2^CACHE_ADDR_WIDTH 拍):
  置 clear_cache 后，wr_ptr 从 0 自增到满，逐槽写 valid=0；
  清空期间查询/写入的 ready 被拉低（暂停服务）。
```

表大小为 \(2^{\text{CACHE\_ADDR\_WIDTH}}\)，默认 `CACHE_ADDR_WIDTH=9` 即 512 个槽。

#### 4.1.3 源码精读

**三组并行存储器**就是这张表的全部数据：[rtl/arp_cache.v:81-83](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_cache.v#L81-L83)

```verilog
reg valid_mem[(2**CACHE_ADDR_WIDTH)-1:0];        // 有效位
reg [31:0] ip_addr_mem[(2**CACHE_ADDR_WIDTH)-1:0]; // 完整 IP（冲突判别）
reg [47:0] mac_addr_mem[(2**CACHE_ADDR_WIDTH)-1:0]; // MAC
```

注意 `ip_addr_mem` 的存在就是为了在直接映射下判别「槽里的 IP 是不是我要的那个」。

**哈希函数**就是两个 `lfsr` 实例（读/写各一），参数正是 CRC-32：[rtl/arp_cache.v:104-118](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_cache.v#L104-L118)

```verilog
lfsr #(.LFSR_WIDTH(32), .LFSR_POLY(32'h4c11db7),
       .LFSR_CONFIG("GALOIS"), .REVERSE(1), .DATA_WIDTH(32))
rd_hash (
    .data_in(query_request_ip),      // 输入：要查的 IP
    .state_in(32'hffffffff),          // CRC 初值
    .state_out(query_request_hash));  // 输出：32 位哈希
```

取哈希的低位作索引，于是查询的 `rd_ptr` 来自 `query_request_hash`：[rtl/arp_cache.v:173-177](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_cache.v#L173-L177)

```verilog
if (query_request_valid && query_request_ready && (...空闲条件...)) begin
    store_query = 1;                          // 锁存 query_ip
    query_ip_valid_next = 1;                  // 下一拍进入"读"阶段
    rd_ptr_next = query_request_hash[CACHE_ADDR_WIDTH-1:0]; // 取低位索引
end
```

**命中/未命中判定**就一句双重条件——有效**且** IP 相等才算命中：[rtl/arp_cache.v:163-171](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_cache.v#L163-L171)

```verilog
if (valid_mem[rd_ptr_reg] && ip_addr_mem[rd_ptr_reg] == query_ip_reg) begin
    query_response_error_next = 0;   // 命中
end else begin
    query_response_error_next = 1;   // 未命中（含"槽有效但 IP 不符"的冲突）
end
```

**写入**与查询对称，`wr_ptr` 同样取哈希低位，无条件写入：[rtl/arp_cache.v:188-192](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_cache.v#L188-L192) 与 [rtl/arp_cache.v:238-242](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_cache.v#L238-L242)

```verilog
// 锁存写入
if (write_request_valid && write_request_ready) begin
    store_write = 1;
    write_ip_valid_next = 1;
    wr_ptr_next = write_request_hash[CACHE_ADDR_WIDTH-1:0]; // 覆盖式写入的槽
end
...
// 下一拍真正落盘
if (mem_write) begin
    valid_mem[wr_ptr_reg] <= !clear_cache_reg; // 清空时写 0
    ip_addr_mem[wr_ptr_reg] <= write_ip_reg;
    mac_addr_mem[wr_ptr_reg] <= write_mac_reg;
end
```

> 这就是「不是 LRU」的铁证：写入路径里**没有任何**比较新旧、计次或选路的逻辑，新 IP 直接覆盖 `hash(IP)` 槽，老内容就此丢失。

**清空**靠 `wr_ptr` 自增扫表，每拍清一个槽：[rtl/arp_cache.v:194-201](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_cache.v#L194-L201)

```verilog
if (clear_cache) begin
    clear_cache_next = 1'b1; wr_ptr_next = 0;   // 启动扫描
end else if (clear_cache_reg) begin
    wr_ptr_next = wr_ptr_reg + 1;               // 逐槽推进
    clear_cache_next = wr_ptr_next != 0;        // 绕回 0 即扫描结束
    mem_write = 1;                              // 配合上面写 valid=0
end
```

清空期间，查询和写入的 `ready` 都被 `!clear_cache_next` 门控住（[L158](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_cache.v#L158)、[L181](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_cache.v#L181)），所以服务会被暂停约 \(2^{\text{CACHE\_ADDR\_WIDTH}}\) 拍。

#### 4.1.4 代码实践

官方 testbench `tb/arp_cache` 专门把表调到极小来观察冲突与覆写。

1. **实践目标**：亲眼看到「直接映射覆写」——两个哈希到同一个槽的 IP，后写的会把先写的踢出缓存。
2. **操作步骤**：
   - 配好 cocotb + iverilog（见 u1-l4）。
   - 进入目录运行：`cd tb/arp_cache && make`
   - 打开 [tb/arp_cache/test_arp_cache.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/arp_cache/test_arp_cache.py)。注意它的 `Makefile` 把表设成 `PARAM_CACHE_ADDR_WIDTH := 2`，即只有 4 个槽，故意放大冲突概率。
   - 关注这段（已节选自源码）：先写 `0xc0a80121/0xc0a80122/0xc0a80123`，注释明确写着 `# overwrites 0xc0a80112`；随后查询 `0xc0a80112`，断言 `assert resp.error`（注释 `# not in cache; was overwritten`）。
3. **需要观察的现象**：`0xc0a80112` 原本能查到（`error=0`），但写入与其哈希冲突的 `0xc0a80123` 后，再查 `0xc0a80112` 返回 `error=1`。
4. **预期结果**：仿真通过，日志里能看到 `not in cache; was overwritten` 的查询返回 error。这正好证明替换策略是「覆写」而非 LRU（LRU 不会因为写入第三方而丢失仍在用的表项）。
5. 想验证表大小的影响，可在 Makefile 里改 `PARAM_CACHE_ADDR_WIDTH`（如改成 3 或 4），重新观察哪些 IP 仍冲突、哪些不再冲突。**冲突是否消失待本地验证**（取决于具体 IP 与哈希）。

#### 4.1.5 小练习与答案

**练习 1**：`CACHE_ADDR_WIDTH=9` 时表能存多少条目？若两个 IP 哈希到同一槽，查其中一个时会发生什么？
**答案**：\(2^9 = 512\) 条。查询时 `ip_addr_mem[slot] != query_ip`，触发 `query_response_error=1`（被当作未命中），即使该槽 `valid=1`。

**练习 2**：为什么说本模块「不是 LRU」？请指出源码中能证明这一点的关键事实。
**答案**：写入路径（L188–192、L238–242）只把数据写进 `hash(IP)` 槽，没有任何「使用次数 / 时间戳 / 比较新旧」的逻辑；同一槽的新写无条件覆盖旧值。LRU 需要维护使用顺序状态，本模块完全没有。

**练习 3**：把 `CACHE_ADDR_WIDTH` 从 9 改成 1，最坏情况下缓存的有效命中率会如何变化？
**答案**：表缩到 2 个槽，几乎所有 IP 都会互相冲突，命中率急剧下降、频繁 thrashing；这正说明深度参数直接决定缓存效率。

---

### 4.2 IP→MAC 查询：arp 顶层组装与缓存查询

#### 4.2.1 概念说明

`arp_cache` 只管「查表」，但它不知道「查不到该怎么办」。`arp` 顶层模块就是那个**会主动想办法**的协调者：它对外暴露一对简单的请求/响应接口（「给我这个 IP 的 MAC」），对内则把收帧、发帧、缓存串成一条完整的服务链。

它要做四件事：

1. **被动学习**：任何收到的合法 ARP 帧，都把其中的「发送方 IP→发送方 MAC」写进缓存——不管是请求还是应答。这样别人主动广播时，我们也顺手更新了表。
2. **被动应答**：收到目标是自己 IP 的 ARP 请求，自动回一个 ARP 应答。
3. **主动查询**：上层来查一个 IP，先查缓存；命中就直接回 MAC。
4. **跨网段路由判断**：查询的 IP 不在本子网时，不去解析它本身，而是去解析**网关**的 MAC（因为包要先交给网关）。

第 4 点（未命中后的自动 ARP 请求与重试）留到 4.3 讲，本节聚焦「顶层组装 + 命中查询 + 子网/网关判断」。

#### 4.2.2 核心流程

顶层把三个子模块像搭积木一样连起来，自身几乎没有数据通路逻辑，关键在一大段组合 `always @*` 状态机：

```
            以太网帧入 ──► arp_eth_rx ──► (并行 ARP 字段)
                                              │
                          ┌───────────────────┼────────────────────┐
                          ▼                   ▼                    ▼
                   被动学习写入          判断是否回应答        (供状态机决策)
                   arp_cache.write       arp_eth_tx
                                              │
            上层 arp_request ──► 状态机 ──► arp_cache.query ──► arp_response
                            (子网/网关判断)
```

查询一条请求的处理（命中路径）：

```
arp_request_valid & ready
  ├─ IP == 0xffffffff            → 直接回广播 MAC（特殊捷径）
  ├─ 在本子网且是子网广播地址     → 直接回广播 MAC
  ├─ 在本子网（单播）             → cache.query(arp_request_ip)
  └─ 不在本子网                   → cache.query(gateway_ip)   # 改查网关
cache 命中 → arp_response_valid + mac
cache 未命中 → 进入 4.3 的"自动请求"流程
```

#### 4.2.3 源码精读

顶层例化三个子模块，把缓存的两套接口接出来：[rtl/arp.v:229-250](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp.v#L229-L250)

```verilog
arp_cache #(.CACHE_ADDR_WIDTH(CACHE_ADDR_WIDTH)) arp_cache_inst (
    .clk(clk), .rst(rst),
    // 查询
    .query_request_valid(cache_query_request_valid_reg),
    .query_request_ip(cache_query_request_ip_reg),
    .query_response_valid(cache_query_response_valid),
    .query_response_error(cache_query_response_error),
    .query_response_mac(cache_query_response_mac),
    .query_response_ready(1'b1),                 // 顶层永远"准备好"收响应
    // 写入
    .write_request_valid(cache_write_request_valid_reg),
    .write_request_ip(cache_write_request_ip_reg),
    .write_request_mac(cache_write_request_mac_reg),
    .clear_cache(clear_cache));
```

**被动学习**写在一开头——只要帧合法就写缓存，与「是否要回应答」无关：[rtl/arp.v:298-302](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp.v#L298-L302)

```verilog
if (incoming_eth_type == 16'h0806 && incoming_arp_htype == 16'h0001
    && incoming_arp_ptype == 16'h0800) begin
    // store sender addresses in cache
    cache_write_request_valid_next = 1'b1;
    cache_write_request_ip_next   = incoming_arp_spa;   // 发送方 IP
    cache_write_request_mac_next  = incoming_arp_sha;   // 发送方 MAC
    ...
```

这段紧跟着「若是发给自己的请求，就组一个应答帧」（`ARP_OPER_ARP_REPLY`），把请求方地址填进应答的目标字段——这正是 u6-l1 讲过的「请求与应答共用同一个 `arp_eth_tx`」。

**子网/网关判断**是查询路径的精华，用位运算一句句表达：[rtl/arp.v:386-412](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp.v#L386-L412)

```verilog
if (arp_request_ip == 32'hffffffff) begin
    // 全网广播 IP → 广播 MAC
    arp_response_mac_next = 48'hffffffffffff;
end else if (((arp_request_ip ^ gateway_ip) & subnet_mask) == 0) begin
    // 在本子网：请求 IP 与网关 IP 在掩码位上完全相同
    if (~(arp_request_ip | subnet_mask) == 0) begin
        // 子网广播地址 → 广播 MAC
        arp_response_mac_next = 48'hffffffffffff;
    end else begin
        // 本子网单播 → 直接查这个 IP
        cache_query_request_valid_next = 1'b1;
        cache_query_request_ip_next    = arp_request_ip;
    end
end else begin
    // 跨网段 → 改查网关的 MAC
    cache_query_request_ip_next = gateway_ip;
end
```

两个位运算判断的含义：

- 「在本子网」：\(((\text{ip} \oplus \text{gateway})\ \&\ \text{mask}) = 0\)，即在所有掩码为 1 的网络位上，二者相同。
- 「子网广播」：\(\sim(\text{ip}\ |\ \text{mask}) = 0\)，即所有主机位（掩码为 0 的位）全为 1。

注意 `arp_request_ip_next` 在单播分支里存的是查询 IP，在跨网段分支里存的是 `gateway_ip`——后续重试发的 ARP 请求（4.3）就以它为目标 IP。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：验证「跨网段查询会被改写成查网关」。
2. **操作步骤**：读 [rtl/arp.v:386-412](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp.v#L386-L412)。假设配置 `local_ip=192.168.1.10`、`subnet_mask=255.255.255.0`、`gateway_ip=192.168.1.1`。
3. **需要观察的现象**：
   - 查 `192.168.1.20` → 落入「在本子网单播」分支，`cache_query_request_ip` = `192.168.1.20`。
   - 查 `8.8.8.8` → 落入「跨网段」分支，`cache_query_request_ip` = `192.168.1.1`（网关）。
   - 查 `192.168.1.255` → 落入「子网广播」，直接返回广播 MAC，不查缓存。
4. **预期结果**：你的分析与上面三条一致。可用手算 \((\text{ip}\oplus\text{gateway})\&\text{mask}\) 复核前两条。
5. 这是纯阅读实践，无需运行仿真；若想跑，可在 `tb/arp/test_arp.py` 里构造对应 `arp_request_ip` 观察内部信号（具体信号是否便于探测待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `arp_cache_inst` 的 `query_response_ready` 接常 `1'b1`？这会不会丢响应？
**答案**：顶层每个时钟都愿意收响应，且状态机一旦看到 `query_response_valid` 就立即消费并转入下一步（命中回 MAC / 未命中进重试）。只要状态机每拍都在看，就不会丢；这也是为何响应只维持一拍。

**练习 2**：被动学习写入的 `(SPA, SHA)` 是否会在「自己刚发出请求、还没收到应答」时就被缓存？
**答案**：不会从自己的请求中学（自己发的帧从 TX 侧出去，不进 RX）。但当**对端**的应答（或任何请求）到达 RX 侧时，只要帧合法，发送方地址就会被写入缓存——这正是应答一来就能命中查询的原因。

---

### 4.3 自动请求与重试：未命中后的 ARP 解析

#### 4.3.1 概念说明

如果上层来查一个 IP，缓存里没有怎么办？`arp` 不会直接报错，而是**主动发起一次 ARP 解析**：广播一个 ARP 请求，等对端应答。应答一到（被动学习写进缓存），重查缓存就命中了。

但网络不可靠——请求可能丢、对端可能不在线。所以还要有**重试**和**超时**：

- **重试**：间隔一段时间没等到应答，就再广播一次请求，最多发 `REQUEST_RETRY_COUNT` 次。
- **超时**：把所有重试用完仍无应答，才向上层报 `error`，表示「这个 IP 解析失败」。

这套机制由一个一位的状态标志 `arp_request_operation_reg`（0=空闲/初次查询，1=正在解析重试中）驱动，加上一个重试计数器和一个倒计时定时器。

#### 4.3.2 核心流程

```
空闲态 (operation=0)，收到 arp_request:
  发起 cache.query
  ├─ 命中(error=0) → 回 response(MAC)        # 完事
  └─ 未命中(error=1) → 进入"解析态":
        发第 1 个 ARP 请求(广播)
        retry_cnt ← REQUEST_RETRY_COUNT - 1
        timer    ← REQUEST_RETRY_INTERVAL

解析态 (operation=1)，每拍:
  再查一次 cache            # 应答可能已被被动学习写入
  timer 减 1
  ├─ cache 命中          → 回 response(MAC)，回空闲      # 应答到了
  └─ timer==0:
        ├─ retry_cnt > 0 → 再发一个请求，retry_cnt--
        │                  timer ← (cnt>1)?INTERVAL:TIMEOUT
        └─ retry_cnt ==0 → 回 response(error=1)，回空闲   # 彻底失败
```

把默认值代入（时钟 125 MHz）梳理重试时间线：

| 事件 | retry_cnt | 动作 | 重载 timer |
| --- | --- | --- | --- |
| 初次未命中，进解析态 | 3（=COUNT−1） | 发请求 #1 | INTERVAL（≈2 s） |
| timer 归零 | 3→2 | 发请求 #2 | INTERVAL（3>1） |
| timer 归零 | 2→1 | 发请求 #3 | INTERVAL（2>1） |
| timer 归零 | 1→0 | 发请求 #4 | TIMEOUT（1 不>1，≈30 s） |
| timer 归零 | 0 | 不再发，返回 error | — |

即默认会发 4 次 ARP 请求（= `REQUEST_RETRY_COUNT`），前 3 次间隔约 2 s，最后一次后等约 30 s 才宣告失败，最坏总耗时约 36 s。定时器寄存器 36 位（[rtl/arp.v:261-262](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp.v#L261-L262)），足以容纳 \(1.25\times10^8 \times 30 \approx 3.75\times10^9 < 2^{36}\)。

#### 4.3.3 源码精读

**进入解析态**发生在初次查询未命中时：[rtl/arp.v:368-378](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp.v#L368-L378)

```verilog
if (cache_query_response_error) begin
    arp_request_operation_next = 1'b1;            // 切到解析态
    // 立刻发第一个 ARP 请求（广播）
    outgoing_frame_valid_next   = 1'b1;
    outgoing_eth_dest_mac_next  = 48'hffffffffffff; // 广播
    outgoing_arp_oper_next      = ARP_OPER_ARP_REQUEST;
    outgoing_arp_tpa_next       = arp_request_ip_reg;
    arp_request_retry_cnt_next  = REQUEST_RETRY_COUNT-1; // 还能再发 3 次
    arp_request_timer_next      = REQUEST_RETRY_INTERVAL; // 2 s 后没应答再发
end
```

**解析态主循环**——每拍都重查缓存（应答随时可能被学习写入），同时倒计时：[rtl/arp.v:328-363](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp.v#L328-L363)

```verilog
if (arp_request_operation_reg) begin
    cache_query_request_valid_next = 1'b1;          // 每拍重查缓存
    arp_request_timer_next = arp_request_timer_reg - 1;
    // 应答到达、被动学习写入缓存后，重查就命中了
    if (cache_query_response_valid && !cache_query_response_error) begin
        arp_request_operation_next = 1'b0;          // 回空闲
        arp_response_valid_next    = 1'b1;
        arp_response_mac_next      = cache_query_response_mac; // 成功
    end
    // 定时器到点
    if (arp_request_timer_reg == 0) begin
        if (arp_request_retry_cnt_reg > 0) begin
            // 还有重试机会 → 再广播一次
            outgoing_eth_dest_mac_next = 48'hffffffffffff;
            outgoing_arp_oper_next     = ARP_OPER_ARP_REQUEST;
            outgoing_arp_tpa_next      = arp_request_ip_reg;
            arp_request_retry_cnt_next = arp_request_retry_cnt_reg - 1;
            if (arp_request_retry_cnt_reg > 1)
                arp_request_timer_next = REQUEST_RETRY_INTERVAL; // 继续 2 s 间隔
            else
                arp_request_timer_next = REQUEST_TIMEOUT;        // 最后一次等 30 s
        end else begin
            // 用尽重试 → 失败
            arp_request_operation_next = 1'b0;
            arp_response_valid_next    = 1'b1;
            arp_response_error_next    = 1'b1;
        end
    end
end
```

三个参数的物理意义（默认值见 [rtl/arp.v:44-50](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp.v#L44-L50)）：

- `REQUEST_RETRY_COUNT = 4`：总共发出的 ARP 请求数（含第一次）。
- `REQUEST_RETRY_INTERVAL = 125000000*2`：前几次重试的间隔（125 MHz 下约 2 秒）。
- `REQUEST_TIMEOUT = 125000000*30`：最后一次请求后的最终等待（约 30 秒），到点即报错。

#### 4.3.4 代码实践

官方 testbench `tb/arp` 已经把这几个长参数调小，方便在几微秒内跑完整条「重试→超时→报错」路径。

1. **实践目标**：观察「未命中 → 连续广播 ARP 请求 → 用尽重试 → 返回 error」的全过程。
2. **操作步骤**：
   - 看 [tb/arp/Makefile](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/arp/Makefile)，它已经设了：
     ```
     PARAM_REQUEST_RETRY_COUNT := 4
     PARAM_REQUEST_RETRY_INTERVAL := 300     # 300 拍 ≈ 2.4 µs（替代 2 s）
     PARAM_REQUEST_TIMEOUT := 800            # 800 拍 ≈ 6.4 µs（替代 30 s）
     PARAM_CACHE_ADDR_WIDTH := 2
     ```
   - 运行 `cd tb/arp && make`，并在 [tb/arp/test_arp.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/arp/test_arp.py) 里找到一个「查询一个无人应答的 IP」的场景（或自行加一个）。
   - 若想直接观察，可在仿真里把 `WAVES=1` 打开波形，盯 `outgoing_frame_valid`（发请求）与 `arp_response_error`（最终报错）。
3. **需要观察的现象**：从发出查询起，大约每隔 300 拍会冒出一个广播 ARP 请求，共 4 次；最后一次之后再过约 800 拍，`arp_response_valid` 拉高且 `arp_response_error=1`。
4. **预期结果**：请求计数 = 4，最终 `arp_response_error=1`。如果你想自己改参数，例如把 `PARAM_REQUEST_RETRY_COUNT` 改 2，则应只看到 2 次请求。**精确拍数待本地验证**（取决于握手与流水线开销）。

#### 4.3.5 小练习与答案

**练习 1**：为什么解析态里「每拍都重查缓存」而不是「只查一次」？
**答案**：因为我们广播请求后，对端的应答随时可能到达 RX 侧并被被动学习写入缓存。只有持续重查，才能在应答写入的下一拍立刻命中、提前结束重试，而不必傻等定时器。

**练习 2**：把 `REQUEST_RETRY_INTERVAL` 设得很大、`REQUEST_RETRY_COUNT` 保持 4，会有什么副作用？
**答案**：单次解析的失败检测会变得很慢（最坏要等 4 个长间隔 + 超时），上层若依赖快速失败重路由会很卡；好处是对丢包更宽容、更少发广播噪声。这是「响应速度 vs 网络宽容度」的权衡。

**练习 3**：`arp_request_timer_reg` 为什么是 36 位？
**答案**：默认 `REQUEST_TIMEOUT = 125000000*30 = 3.75×10^9`，需要 \(\lceil\log_2(3.75\times10^9)\rceil = 32\) 位以上；留到 36 位既容下最大超时值，也留出用户把它调更大的余量。

---

## 5. 综合实践

把本讲三块知识串起来：用 `tb/arp_cache` 和 `tb/arp` 两个现成 testbench 做一组对比实验，亲手走一遍「缓存命中 / 未命中触发解析 / 重试 / 失败」的完整状态。

任务步骤：

1. **建表与命中**：在 `tb/arp_cache` 里（`CACHE_ADDR_WIDTH` 保持 2），先写 `(0xc0a80111, MAC_A)`，再查同一个 IP，确认 `error=0` 且返回 `MAC_A`。
2. **制造冲突覆写**：写入一个与 `0xc0a80111` 哈希冲突的 IP（参考 testbench 注释里已被验证会覆写的那些地址，如 `0xc0a80121/22/23` 系列），确认再查 `0xc0a80111` 时变成 `error=1`。**用源码事实解释**：为什么没有 LRU 也能解释这个现象。
3. **触发自动解析**：切到 `tb/arp`，配置好 `local_ip` 等，发起一个「缓存里没有、也没有对端应答」的查询（`PARAM_REQUEST_*` 用 Makefile 里的小值）。在波形里数清楚：发出了几次广播 ARP 请求？最终 `arp_response_error` 在第几拍拉高？
4. **让解析成功**：在第 3 步的基础上，用 cocotbext-eth 在 RX 侧注入一个「目标 IP 匹配的 ARP 应答」，确认 `arp` 提前结束重试、返回正确的 MAC（而不是等到超时）。

完成后，你应该能用一句话说清 `arp_cache`（被动数据结构）与 `arp`（主动状态机）的分工：**前者只负责存与查，后者负责在查不到时主动去问、并按参数决定问几次、等多久。**

> 提示：本实践依赖 u1-l4 描述的 cocotb + iverilog 工具链；若尚未配置，可先做步骤 1–2 的纯阅读分析，运行部分待本地验证。

## 6. 本讲小结

- `arp_cache` 是一张**直接映射哈希表**：`index = CRC32(IP)` 的低 `CACHE_ADDR_WIDTH` 位，表大小 \(2^{\text{CACHE\_ADDR\_WIDTH}}\)（默认 512）。它**不是 LRU**，替换策略是无条件覆写，靠存全 IP 来判别冲突。
- 查询与写入都是 2 拍流水线；命中条件是「槽有效 **且** IP 相等」，否则报 `error=1`（未命中，含冲突）。`clear_cache` 用 `wr_ptr` 自增扫表，清空期间暂停查询/写入。
- `arp` 顶层是「布线层 + 一段组合状态机」，例化 `arp_eth_rx`/`arp_eth_tx`/`arp_cache`，对外提供简单的 `arp_request(IP) → arp_response(MAC/error)` 接口。
- 它做四件事：**被动学习**（任何合法 ARP 帧的发送方地址都入缓存）、**被动应答**（收到给自己的请求就回应答）、**子网/网关判断**（跨网段改查网关）、**未命中后自动解析**。
- 自动解析 = 广播请求 + 重试：未命中时发第 1 个请求，`retry_cnt = COUNT-1`；每过 `INTERVAL` 没应答就再发，共发 `REQUEST_RETRY_COUNT` 次；最后一次后等 `REQUEST_TIMEOUT`，仍无应答则 `error=1`。解析态每拍重查缓存，应答一到立即命中结束。
- 默认值在 125 MHz 下：4 次请求、约 2 s 间隔、约 30 s 最终超时；testbench 用 `PARAM_REQUEST_RETRY_INTERVAL=300` 等小值把它们缩到微秒级以便仿真。

## 7. 下一步学习建议

- 本讲结束后，ARP 子系统已完整。下一站进入 **u7（IPv4 层）**：从 `ip_eth_rx/ip_eth_tx` 的 IPv4 头解析与校验和开始，注意 `ip` 核心模块正是通过本讲的 `arp` 模块来完成「IP→MAC」查找的——你会看到 `arp_request/arp_response` 接口被 IP 层直接调用。
- 想加深对哈希的理解，可回看 **u2-l1（lfsr）**，并思考：为什么作者选 CRC-32 而不是简单的取模做哈希？换成取模对相邻 IP 的分布会有什么影响？
- 想看 ARP 在真实系统里如何被接线，可跳到 **u12-l1（组装完整 UDP 回显系统）**，阅读 `example/Arty` 里 `arp_complete`/`ip_complete` 如何把本讲的 `arp` 与 IP 层、MAC 层连成一条端到端通路。
- 若你对「替换策略」感兴趣，可作为一个扩展练习：尝试把 `arp_cache` 改成 2 路组相联 + 真 LRU，对比冲突率与资源占用（注意：这属于二次开发，不在本讲范围内，且不要修改仓库源码，可在自己的副本上实验）。
