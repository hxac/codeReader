# 共享 pin memory 池:跨 checkpoint 的内存复用

## 1. 本讲目标

学完本讲,你应该能够:

1. 说清 `shared_memory_pool_name`、`_memory_pool`、`_current_shared_memory_pool_user` 三个状态各自的含义,以及「空列表表示共享池尚未定型」这一约定。
2. 读懂 `register_checkpoint(use_shared_memory_pool=True)` 分支:单一使用者断言、首次判定、失败回滚为什么不会破坏池。
3. 掌握 `pin_memory.py` 中内层函数 `register_pin_memory` 的复用条件——桶数量相同且逐桶字节数相等——并能手工推演一个 checkpoint 能否复用已有池。
4. 解释 `unregister_checkpoint(force=...)` 的两种语义:非 force 只「让位」,force 才真正释放内存;并理解「让位之后就无法再 force」这一陷阱。
5. 说明共享池与 p2p store 注册的交互:为什么只在首次注册、为什么注册名与 checkpoint 名无关。

## 2. 前置知识

本讲建立在 u2-l3(锁页内存与两种 pin 策略)之上,先快速回顾三个结论:

- **锁页内存(pinned memory)很贵**:把一段 host 内存锁页需要驱动逐页登记,大模型动辄几十上百 GB,`torch.empty(..., pin_memory=True)` 的分配 + 锁页成本可观。所以本项目把 pin 放在 `register_checkpoint` 阶段一次性完成,`update` 阶段只受益。
- **normal pin 的产物是 `MemoryBuffer` 列表**:每个 `MemoryBuffer` 持有一个扁平的 uint8 锁页 buffer、一组按排布顺序记录的 `ParameterMeta`(`offset` 由 `aligned_size` 逐个累加隐含)和一个 `manually_pinned` 标志,见 [checkpoint_engine/data_types.py:L96-L100](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L96-L100)。
- **桶布局是确定性的**:`_normal_pin_memory` 把参数按名字排序后,以 `bucket_size = max(4GiB, 最大张量 aligned_size)` 为软上限贪心装箱,布局只由「参数名集合 + 各自的 dtype/shape」决定,与权重数值无关。

再回顾 u1-l2 建立的生命周期:`register_checkpoint → gather_metas → update → unregister_checkpoint`。

本讲要回答的新问题是:**RL 训练循环每个迭代都会产生一个新 checkpoint,每次都重新分配一大块锁页内存、用完再释放,值得吗?** 答案是不值得——同一模型结构的相邻两代 checkpoint,布局完全相同,只是数值不同。于是项目提供了一个选项:`use_shared_memory_pool=True`,让第一次分配的锁页 buffer 跨 checkpoint 存活,后续注册只是把新权重**拷进旧 buffer**。

一个术语约定:后文用「让位」指 `unregister_checkpoint` 非 force 地清除使用者标记(池保留),用「释放」指 `force=True` 真正归还内存。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [checkpoint_engine/ps.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py) | ParameterServer 服务端 | 三个状态字段、`register_checkpoint` / `unregister_checkpoint` 的共享池分支、`_register_parameters_to_p2p_store` |
| [checkpoint_engine/pin_memory.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py) | checkpoint 加载与锁页 | `_normal_pin_memory` 内层函数 `register_pin_memory` 的复用/分配分岔 |
| [tests/test_reuse_pin_memory.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_reuse_pin_memory.py) | 共享池行为的验收测试 | 一整条注册/注销操作序列对应的状态断言 |
| [checkpoint_engine/data_types.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py) | 核心数据模型 | `MemoryBuffer` 的结构(复用的最小单元) |

## 4. 核心概念与源码讲解

### 4.1 问题背景与三个状态字段

#### 4.1.1 概念说明

设想参数服务器的进程生命周期内有这样一串操作:

```text
register(ckpt_v1) → update → unregister(ckpt_v1)
register(ckpt_v2) → update → unregister(ckpt_v2)
register(ckpt_v3) → update → unregister(ckpt_v3)
...
```

不开共享池时,每个 `register` 都要执行一次「分配 + 锁页」,每个 `unregister` 都把几百 GB 的锁页内存还给系统。这有两个代价:一是锁页本身慢,二是反复大块申请/归还后,主机内存碎片和分配器行为都不可控。

共享池的思路非常朴素:**只要每次的形状一样,buffer 就不必还,下一轮直接往里写**。用公式表达,设第 \( j \) 次注册得到的桶大小序列为 \( S^{(j)} = (S^{(j)}_0, S^{(j)}_1, \dots, S^{(j)}_{n_j-1}) \),则复用合法当且仅当

\[
n_j = n_{j'} \ \wedge\ \forall k \in [0, n_j):\ S^{(j)}_k = S^{(j')}_k
\]

即**桶的数量相同,且每个下标位置上的桶字节数相等**。注意约束的是字节数序列,不是参数名——但因为布局由排序后的名字和形状共同决定,实践中「同名同形」是满足该条件的充分条件。

为了支持这个机制,`ParameterServer` 用三个状态管理共享池。

#### 4.1.2 核心流程

```text
ParameterServer.__init__
  ├─ _memory_pool = {}                                  # 普通 checkpoint:name → list[MemoryBuffer]
  ├─ _memory_pool["__shared_memory_pool__"] = []        # 保留键,恒存在;空列表 = 尚未定型
  └─ _current_shared_memory_pool_user = ""              # 当前占用者;空串 = 无人使用
```

读取侧的统一入口是 `_get_memory_pool(checkpoint_name)`:

```text
name == _current_shared_memory_pool_user ?
  ├─ 是 → 返回 _memory_pool["__shared_memory_pool__"](并断言非空)
  ├─ 否,name 在 _memory_pool 中 → 返回该 checkpoint 的专属池
  └─ 否 → RuntimeError("checkpoint ... is not registered")
```

`gather_metas`、`update`、p2p 注册全部经过这个入口,因此**下游代码完全不感知共享与否**。

#### 4.1.3 源码精读

保留键是一个类属性,用双下划线包围来避免与真实 checkpoint 名冲突:

- [checkpoint_engine/ps.py:L177](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L177) 定义 `shared_memory_pool_name = "__shared_memory_pool__"`。

初始化发生在构造函数里,注释明确写了「空串表示无人使用」:

- [checkpoint_engine/ps.py:L224-L227](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L224-L227) 初始化 `_current_shared_memory_pool_user = ""` 与 `_memory_pool`,并立刻给保留键塞入空列表。

`_get_memory_pool` 是三路分发,注意第一路的断言:如果某个名字声称在用共享池而池却是空的,说明状态被破坏了,立即报错而不是返回空列表:

- [checkpoint_engine/ps.py:L277-L286](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L277-L286) 共享使用者返回共享池、普通名字返回专属池、未知名字抛 `RuntimeError`。

「空列表 = 尚未定型」这个约定贯穿全机制:它既是「首次判定」的依据(4.2.3),也是「复用分岔」的开关(4.3.3),还是 force 释放后的复位目标(4.4.3)。

#### 4.1.4 代码实践:git 考古

1. **实践目标**:确认共享池机制是何时、以何种粒度进入项目的。
2. **操作步骤**(只读 git 命令,纯 CPU 可执行):

```bash
git log --oneline -S "shared_memory_pool_name" -- checkpoint_engine/ps.py
git show 6b9ffc7 --stat          # 复用机制引入
git show f69e116 --stat          # force 释放语义引入
```

3. **需要观察的现象**:第一条命令应输出两个提交——`6b9ffc7 feat: reuse pin_memory when registering checkpoint (#56)` 与 `f69e116 feat: force unregister shared pin memory buffer supported (#62)`。
4. **预期结果**:你会发现「复用」和「force 释放」是**两个独立提交**——先有复用,后来才补上「如何真正释放」的出口。这正好对应本讲 4.3 与 4.4 两节的主题。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `_current_shared_memory_pool_user` 的空状态用空字符串 `""` 而不是 `None`?

**答案**:这个值要同时参与两类判断:与 checkpoint 名做相等比较(`checkpoint_name != self._current_shared_memory_pool_user`),以及做「是否无人使用」的断言(`assert self._current_shared_memory_pool_user == ""`)。真实 checkpoint 名永远不等于空串,所以空串是一个天然的哨兵值;而如果用 `None`,相等比较和布尔判断就要分写成两种形式,反而容易漏判。

**练习 2**:`_get_memory_pool` 第一路的断言防的是什么 bug?

**答案**:防止「使用者标记还在、池却是空的」这种状态错乱——例如 force 释放时只删了池却忘了清标记。断言让这种不一致在读取时立刻炸出来,而不是把空列表静默传给下游,导致后续在难以定位的地方(比如广播时取 `pool[b.idx]`)才出错。

**练习 3**:普通 checkpoint 的 buffer 列表以 checkpoint 名为键存在 `_memory_pool` 里,共享池为什么不能用这种「名字即键」的方式?

**答案**:共享池的生命周期跨越多个 checkpoint。如果以使用者的名字为键,「让位」时就得决定是删键(内存没法复用)还是保留一个名不副实的键(下一个使用者会覆盖)。用一个固定的保留键 + 一个单独的「当前使用者」标记,把「内存放在哪」和「谁在用」两个正交问题拆开,状态转换最简单。

### 4.2 register_checkpoint:注册分支与单一使用者约束

#### 4.2.1 概念说明

`register_checkpoint` 是共享池的唯一写入口。它要做四件事:确认没有别人在用共享池(单一使用者约束)、判断是否首次使用(决定分配还是复用)、调用底层 `_register_checkpoint` 完成加载、成功后把自己记为使用者。

单一使用者约束的必要性:共享池物理上只有一份 buffer,如果两个 checkpoint 同时声称在用,那么后注册者的数据会覆盖先注册者的——而 `update` 广播可能还在读先注册者的数据。与其默许数据竞争,项目选择在注册期就 `AssertionError` 拒绝。

#### 4.2.2 核心流程

```text
register_checkpoint(name, use_shared_memory_pool=True)
  ├─ assert _current_shared_memory_pool_user == ""        # 单一使用者
  ├─ _is_first_time = 共享池列表为空                      # 空列表 = 尚未定型
  ├─ _memory_pool["__shared_memory_pool__"] = _register_checkpoint(
  │       files, named_tensors, rank,
  │       shared_pin_memory=_memory_pool["__shared_memory_pool__"],  # 旧池(首次为 [])
  │       inplace_pin=False)                             # 共享池与 inplace pin 不兼容
  ├─ _current_shared_memory_pool_user = name
  └─ 若 p2p_store 存在且 _is_first_time:
        _register_parameters_to_p2p_store(name)          # 只在首次注册远端内存
── 异常路径 ──
  └─ logger.exception → (仅非 shared 分支回滚 p2p) → unregister_checkpoint(name)
```

两个关键点:

- **赋值是原子的**。`self._memory_pool[保留键] = _register_checkpoint(...)` 只有函数正常返回才发生赋值;`_register_checkpoint` 内部从不修改传入的旧列表,而是返回新列表。因此失败时旧池(或空列表)原封不动。
- **回滚是安全的**。异常发生时 `_current_shared_memory_pool_user` 还没被改写(它在调用成功之后才赋值),所以回滚里的 `unregister_checkpoint(name)` 会发现 `name` 既不在 `_memory_pool` 也不是当前使用者,走「not found 警告 + return」,池完好无损。

#### 4.2.3 源码精读

docstring 把约束写得非常清楚——形状首次固定、同时只能一个使用者、要释放或改形状必须 force:

- [checkpoint_engine/ps.py:L323-L329](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L323-L329) `use_shared_memory_pool` 参数文档,并注明该模式下 `use_inplace_pin_memory` 被忽略。

共享分支主体,单一使用者断言 + 首次判定 + 调用 + 登记使用者:

- [checkpoint_engine/ps.py:L337-L358](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L337-L358) 注册共享池使用者。L341-L345 是单一使用者断言;L346-L348 用「列表是否为空」判定首次;L349-L355 调用 `_register_checkpoint` 并传入旧池,且 `inplace_pin=False`(源码注释:inplace pin 与共享池不兼容);L357-L358 只在首次且 p2p store 可用时向 p2p store 注册。

为什么共享池必须关掉 inplace pin?回顾 u2-l4:inplace 路径用 `torch.from_file` mmap `/dev/shm` 下的 safetensors 文件、`cudaHostRegister` 原地锁页,然后**删掉源文件**,buffer 布局即文件布局。这条路径根本没有「把新权重拷进已有 buffer」的步骤——数据天然就在锁页后的位置上。而共享池的核心动作恰恰是「复用旧 buffer + 拷入新数据」,两者结构上不兼容; moreover inplace 的每个文件独占一个 `MemoryBuffer`,桶粒度即文件粒度,无法保证跨代的布局稳定。

失败回滚:

- [checkpoint_engine/ps.py:L371-L378](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L371-L378) 捕获一切异常后记录日志;注意 L375 的条件 `not use_shared_memory_pool`——共享分支失败时**不**回滚 p2p 注册。原因有二:一是共享分支的 p2p 注册发生在成功路径的末尾,失败时根本没注册过;二是共享池在 p2p store 里的注册名与 checkpoint 名无关(见 4.5),即便部分成功也不会留下以该名字命名的残留。随后 `unregister_checkpoint(name)` 对一个未登记的名字只会打警告。

对照普通分支(不开共享池),可以看到两条路径的对称性:

- [checkpoint_engine/ps.py:L359-L368](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L359-L368) 断言名字未重复注册,以 checkpoint 名为键存入池,并(若 p2p 可用)每次都注册到 p2p store——注意与共享分支「仅首次」的差异。

#### 4.2.4 代码实践:读测试,推状态

1. **实践目标**:把 `tests/test_reuse_pin_memory.py` 的每一步操作映射到 `_memory_pool` 的键集合与 `_current_shared_memory_pool_user` 的取值。
2. **操作步骤**(纯 CPU 可执行的部分):

```bash
pytest tests/test_reuse_pin_memory.py --collect-only -q
```

   确认测试可被收集(不会真正执行,不需要 GPU)。然后通读 [tests/test_reuse_pin_memory.py:L22-L79](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_reuse_pin_memory.py#L22-L79),边读边填下面这张表的第三、四列。

3. **需要观察的现象 / 预期结果**(参考答案,即状态推演表):

| 测试行 | 操作 | `_memory_pool` 的键 | `_current_shared_memory_pool_user` |
|---|---|---|---|
| L36-L37 | register/unregister `test_checkpoint1`(普通) | `{__shared_memory_pool__: []}` | `""` |
| L39-L41 | register `test_checkpoint_shared1`(共享) | `{__shared_memory_pool__: [buffer...]}` | `"test_checkpoint_shared1"` |
| L42-L46 | register `test_checkpoint2`(普通) | 追加 `test_checkpoint2` | 不变 |
| L47-L54 | register `test_checkpoint_shared2`(共享)→ `AssertionError` | **不变** | **不变** |
| L55-L57 | unregister `shared1`(非 force,让位) | 不变(池保留) | `""` |
| L58-L63 | register `shared2`(共享,同形状) | **不变**(没有新增 `shared2` 键!) | `"test_checkpoint_shared2"` |
| L64-L65 | unregister `test_checkpoint1` → 警告 | 不变 | 不变 |
| L68-L70 | unregister `shared2` **force=True** | `{__shared_memory_pool__: []}`(池被删后重置) | `""` |
| L71-L76 | register `shared3`(共享,**更大形状**) | `{__shared_memory_pool__: [新 buffer]}` | `"test_checkpoint_shared3"` |
| L77-L79 | unregister `shared3` | 不变 | `""` |

   特别注意 L61-L62 的断言 `assert "test_checkpoint_shared2" not in ps._memory_pool`:复用成功时**不会**以 checkpoint 名建新键,这是判断「真的复用了」的直接证据。L64 的注释 "this will trigger an warning" 对应 4.4 节的 not-found 路径。
4. 上述推演在无 GPU 环境下为纯阅读结论,**运行验证待本地验证**(测试带 `gpu` marker)。

#### 4.2.5 小练习与答案

**练习 1**:如果把 L47-L50 的 `try/except AssertionError` 去掉,测试会在哪一行死掉,为什么?

**答案**:死在 L48-L50 的 `register_checkpoint` 调用本身,触发 [checkpoint_engine/ps.py:L341-L345](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L341-L345) 的单一使用者断言(`shared1` 还在用池)。异常向上传播,测试失败。

**练习 2**:注册失败后,回滚代码为什么不会误伤共享池里**上一位使用者**留下的数据?

**答案**:因为断言/加载失败发生在 `_current_shared_memory_pool_user = checkpoint_name` 赋值**之前**,且回滚里的 `unregister_checkpoint(name)` 对这个新名字走的是 not-found 警告路径,根本碰不到共享池;同时 `_memory_pool[保留键] = ...` 的赋值尚未发生,旧列表引用未变。另外单一使用者约束保证了尝试注册时池没有活跃使用者,所以即便底层多线程拷贝写坏了部分内容,也没有人在读它。

**练习 3**:共享分支为什么不像普通分支那样每次都注册 p2p store?

**答案**:p2p store 注册的是 (data_ptr, size)(见 4.5 节)。复用模式下地址和大小都不变,重复注册是纯冗余;而权重内容在原地被新数据覆盖,远端按旧地址读到的自然就是新权重。所以只在 `_is_first_time` 时注册一次。

### 4.3 register_pin_memory:复用的形状约束

#### 4.3.1 概念说明

`register_pin_memory` 是 `_normal_pin_memory` 里的一个内层函数,每个桶调用一次,负责「拿到这个桶对应的锁页 buffer」。它是共享池机制的真正执行点:PS 传进来的 `shared_pin_memory` 非空就复用旧 buffer,为空就分配新的。

最巧妙的一点:**复用与否的分岔只是一个真值判断**。首次使用时 PS 传入空列表 `[]`(Python 中为 falsy),函数自然走分配分支;返回的 `MemoryBuffer` 列表被写回 `_memory_pool` 后,下一轮传入的就是非空列表,自动切换到复用分支。整个机制不需要额外的「是否初始化」标志位——池的空/非空本身就是状态。

还要澄清一个容易误解的点:**复用的只有底层 buffer 张量,`metas` 每次都重建**。`_normal_pin_memory` 每次都会重新加载 checkpoint、重新排序、重新装箱,生成全新的 `ParameterMeta` 列表;`register_pin_memory` 只是把旧 buffer 填进新的 `MemoryBuffer`。也就是说,「哪些参数、放在 buffer 的哪个 offset」由本轮 checkpoint 决定,「哪块物理内存」由第一次决定。

#### 4.3.2 核心流程

```text
register_pin_memory(idx, size, shared_pin_memory)
  ├─ if shared_pin_memory:            # 非空列表 → 复用
  │    assert idx < len(shared_pin_memory)          # 桶数量约束
  │    assert shared_pin_memory[idx].size == size   # 逐桶字节数约束
  │    return idx, shared_pin_memory[idx].buffer    # 返回旧 buffer,不分配
  └─ else:                             # 空列表/None → 分配
       buffer = torch.empty(size, dtype=uint8, pin_memory=True)
       return idx, buffer
```

调用侧(多线程)对每个桶提交一个 `register_pin_memory` 任务;每个任务完成后,主线程立刻把该桶的新张量并发拷入 buffer:

- 复用节省的是「分配 + 锁页」的时间与分配器压力;**磁盘读取与内存拷贝一次都不少**——这是正确性的要求,毕竟新权重的数值必须写进去。

#### 4.3.3 源码精读

函数签名与两分支实现,注意两条断言正是 4.1.1 公式的直接代码化:

- [checkpoint_engine/pin_memory.py:L309-L324](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L309-L324) `register_pin_memory`:L312 `if shared_pin_memory:` 以列表真值分岔(空列表走 else);L314-L321 两条断言约束「下标不越界、逐桶字节数相等」并返回旧 buffer;L322-L324 分配新的锁页 buffer。源码注释写明「复用仅支持首次注册时固定的 checkpoint 形状」。

`metas` 每次重建的证据——`memory_buffers` 先用占位 buffer 构造,`metas` 用的就是本轮 `bucket.metas`:

- [checkpoint_engine/pin_memory.py:L304-L307](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L304-L307) 以 `torch.empty(0)` 占位构造 `MemoryBuffer` 列表,metas 来自本轮装箱结果。

多线程调度与「拿到 buffer 后立刻拷入」的流水:

- [checkpoint_engine/pin_memory.py:L329-L345](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L329-L345) 对每个桶提交 `register_pin_memory`;`as_completed` 中拿到 `(idx, buffer)` 后断言 `buffer.numel() == buckets[idx].size`,写回 `memory_buffers[idx].buffer`,然后按 meta 顺序提交张量拷贝任务。

真正执行「把新权重写进(可能是旧的)buffer」的一行:

- [checkpoint_engine/pin_memory.py:L326-L327](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L326-L327) `register_tensor` 把张量按 offset 切片写入 uint8 buffer。

`_register_checkpoint` 只是把 PS 传入的旧池透传下去,自身不做复用逻辑:

- [checkpoint_engine/pin_memory.py:L365-L401](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L365-L401) 分发函数;L370 声明 `shared_pin_memory` 参数,L390-L398 透传给 `_normal_pin_memory`。注意共享模式下 PS 已把 `inplace_pin` 置 False,所以不会走到 `_inplace_pin_memory` 分支。

#### 4.3.4 代码实践:手算一个布局(纯 CPU)

1. **实践目标**:验证 dummy checkpoint 的桶布局,并判断 4 参数版本与 6 参数版本能否共用一个池。
2. **操作步骤**:在项目根目录执行(仅用到 `_align_size`,不构造 ParameterServer,不需要 GPU):

```bash
python - <<'EOF'
from checkpoint_engine.pin_memory import _align_size
import torch

tensors = {
    "layer1.weight": (1024, 1024), "layer1.bias": (1024,),
    "layer2.weight": (2048, 1024), "layer2.bias": (2048,),
}
total = 0
for name in sorted(tensors):           # 装箱顺序 = 名字排序
    shape = torch.Size(tensors[name])
    size = _align_size(torch.float32, shape)
    total += size
    print(f"{name:16s} aligned = {size}")
print("single bucket size =", total)
EOF
```

3. **需要观察的现象**:按排序后的输出应为 `layer1.bias 4096`、`layer1.weight 4194304`、`layer2.bias 8192`、`layer2.weight 8388608`,单桶总大小 `12595200` 字节(约 12.01 MiB)。由于总量远小于 `bucket_size = max(4GiB, 最大张量)`,四个参数装成一个桶。
4. **预期结果**:测试里新增 `layer3.weight (4096,2048)` 与 `layer3.bias (4096,)` 后单桶总大小变为 `46166016` 字节(约 44.03 MiB)。两次的桶数量都是 1,但 \( S_0 \) 不相等(12595200 ≠ 46166016),不满足复用条件——所以 [tests/test_reuse_pin_memory.py:L68](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_reuse_pin_memory.py#L68) 必须先 `force=True` 释放旧池,L71-L72 才能注册成功。这正是 4.1.1 公式的现实映照。

#### 4.3.5 小练习与答案

**练习 1**:两次注册的参数**名字不同但形状完全一致**,一定能复用吗?

**答案**:不一定,但大概率可以。复用条件是「桶数量相同且逐桶字节数相等」。装箱顺序由名字排序决定,改名会改变排序,进而可能改变张量进桶的组合,导致某个桶的 size 不同(尤其当参数很多、跨越 `bucket_size` 边界时)。小模型单桶场景下只要总 aligned 字节数相等就能复用。稳妥的结论是:**把「同名同形」当作复用的前提**。

**练习 2**:复用路径上,旧的 `MemoryBuffer.metas` 去哪了?

**答案**:被丢弃了。`_normal_pin_memory` 每轮重新构造 `memory_buffers` 列表,metas 用本轮的装箱结果;`register_pin_memory` 只取旧对象的 `.buffer` 字段填进新 `MemoryBuffer`。所以旧 metas 随旧列表一起失去引用(PS 端写回 `_memory_pool` 后旧列表整体被替换)。

**练习 3**:为什么复用分支的断言放在 `register_pin_memory`(pin_memory.py)而不是 `register_checkpoint`(ps.py)?

**答案**:因为「桶大小序列」是在 `_normal_pin_memory` 装箱之后才确定的,PS 层在调用前只知道文件列表/张量字典,不知道布局。把约束检查放在布局产生的地方,断言消息里才能给出具体的 `idx`、期望 size 与实际 size,定位问题最直接。这也体现了分层:ps.py 管生命周期语义(谁在用),pin_memory.py 管内存语义(多大、怎么放)。

### 4.4 unregister_checkpoint:让位与释放的 force 语义

#### 4.4.1 概念说明

`unregister_checkpoint(checkpoint_name, force=False)` 对共享池使用者有两种截然不同的语义:

- **force=False(让位)**:只清空 `_current_shared_memory_pool_user`,然后**直接返回**。池的内存、`_memory_pool` 里的条目、p2p store 里的注册全部原样保留。下一个同形状 checkpoint 注册时零成本接管。
- **force=True(释放)**:走完整注销流程——从 p2p store 注销、清使用者标记、删除池条目并重置为空列表。此后共享池回到「未定型」状态,下一个共享注册可以按新形状重新分配。

为什么需要 force?因为复用机制把「释放内存」的权力从框架手里收走了:正常注销不再归还内存,就必须补一个显式出口,否则形状永远改不了、内存永远收不回——这正是 git 考古里看到的第二个提交 `f69e116 feat: force unregister shared pin memory buffer supported (#62)`。

**一个重要的陷阱**:让位之后就无法再 force 了。非 force 注销把使用者标记清成了 `""`,此后再用原来的名字调 `force=True`,会命中「not found」条件——`name not in _memory_pool`(共享池键不是这个名字)`and name != ""`——只打一条警告就返回,池实际上**永远无法通过这个名字释放**了(直到进程退出)。要释放共享池,必须趁使用者仍然持有它时(`_current_shared_memory_pool_user == name`)调用 `unregister_checkpoint(name, force=True)`。

#### 4.4.2 核心流程

```text
unregister_checkpoint(name, force)
  ├─ name 不在 _memory_pool 且 name != 当前使用者 → 警告 "not found",return
  ├─ name == 当前使用者 且 非 force → 清使用者标记,return     # 让位:池、p2p 注册全保留
  └─ (force,或 name 是普通 checkpoint) → 完整注销:
       ├─ 从 p2p store 注销(共享使用者以 __shared_memory_pool__ 为名)
       ├─ name == 当前使用者?
       │     ├─ 是 → 清标记;del 池条目;重置为 []               # 释放共享池
       │     └─ 否 → 手动 unpin(manually_pinned 者)+ del 专属条目  # 普通 checkpoint(u2-l4 已讲)
       └─ host_empty_cache()
```

#### 4.4.3 源码精读

docstring 对 force 的定义:

- [checkpoint_engine/ps.py:L386-L387](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L386-L387) 「If True, the memory for shared memory pool itself will be freed. If False, only the checkpoint name will be unregistered, and the shared memory pool will be kept for future use.」

not-found 守卫——注意它同时检查 `_memory_pool` 成员关系和使用者标记,覆盖了「从未注册」和「已让位」两种情况:

- [checkpoint_engine/ps.py:L389-L396](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L389-L396) 条件不满足则警告并 return。

让位路径,一行清标记一行 return,注意它**在 p2p 注销代码之前**就返回了:

- [checkpoint_engine/ps.py:L398-L400](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L398-L400) `if checkpoint_name == self._current_shared_memory_pool_user and not force: self._current_shared_memory_pool_user = ""; return`。p2p 注册因此得以保留——内存还活着,注册就该活着。

释放路径:

- [checkpoint_engine/ps.py:L402-L411](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L402-L411) 先从 p2p store 注销;随后若名字是共享使用者,清标记、`del` 池条目、重置为空列表 `[]`——三步把状态完整拨回构造时刻的模样。

普通 checkpoint 的分支(手动 unpin 的细节在 u2-l4 已精读过,这里只看结构):

- [checkpoint_engine/ps.py:L412-L457](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L412-L457) `manually_pinned` 的 buffer 手动解页后 `del self._memory_pool[checkpoint_name]`。

两条路径汇合后的收尾:

- [checkpoint_engine/ps.py:L458-L460](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L458-L460) `host_empty_cache()`:在 CUDA + torch>=2.5 上归还 CachingHostAllocator 缓存的锁页内存,NPU/XPU 回退到 `gc.collect()`。force 释放共享池后,正是这一步让内存真正回到系统。

#### 4.4.4 代码实践:标注三条返回点

1. **实践目标**:在源码上亲手标出 `unregister_checkpoint` 的三个 `return` 点,并验证「让位后 force 失效」的推演。
2. **操作步骤**:
   - 打开 [checkpoint_engine/ps.py:L380-L460](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L380-L460),在 L396(not found)、L400(让位)、函数末尾(完整注销)三处做笔记,写出每个返回点执行过的副作用(p2p 是否注销、池条目是否删除、标记是否清空)。
   - 用状态推演回答:测试 L55 先 `unregister_checkpoint("test_checkpoint_shared1")`(非 force),若接着执行 `unregister_checkpoint("test_checkpoint_shared1", force=True)`,会发生什么?
3. **需要观察的现象 / 预期结果**:第二次调用命中 L389-L391 的守卫——`"test_checkpoint_shared1"` 既不在 `_memory_pool`(共享池的键是 `__shared_memory_pool__`),也不等于 `_current_shared_memory_pool_user`(已被清成 `""`)——于是只打一条 `unregister checkpoint name ... not found` 警告并返回,共享池内存**没有**被释放。对照测试实际写法 L68:`force=True` 是趁着 `shared2` 仍是使用者时调用的,所以能走通释放路径。
4. 本实践为源码阅读型,**运行验证待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**:让位路径为什么不能顺便把 p2p 注册也清掉?反正当前已经没有使用者了。

**答案**:清掉 p2p 注册意味着下一个使用者要重新注册一遍 (ptr, size)。而下一个使用者复用同一块 buffer,地址不变,注册信息完全一样——清了再注册是纯浪费,还会在注册窗口内让远端拓扑(`_remote_rdma_devices`)短暂失效。保留注册的前提正是「内存没释放」,两者必须同步:释放(force)才注销,让位(非 force)则都保留。

**练习 2**:force 释放路径里 `del` 之后为什么还要 `self._memory_pool[self.shared_memory_pool_name] = []`,直接删掉键不行吗?

**答案**:不行。「保留键恒存在、空列表表示尚未定型」是全机制的基石(4.1)。如果删掉键,下一次注册共享池时 `_is_first_time = not self._memory_pool[self.shared_memory_pool_name]` 会直接 `KeyError`。重置为空列表既保住了键的存在性,又把「未定型」状态表达出来,还避免了残留旧引用阻碍内存回收。

**练习 3**:普通 checkpoint 的注销从来不需要 force 参数,为什么共享池需要?

**答案**:普通 checkpoint 的注销语义只有一种:释放。共享池把「名字失效」和「内存失效」解耦了——名字可以反复让位给下一代,内存要尽量长寿。于是注销函数需要第二个维度来表达「这次是真的要内存死」,这就是 force。这是典型的「生命周期拆分带来显式控制」的例子。

### 4.5 与 p2p store 的交互

#### 4.5.1 概念说明

p2p store(基于 mooncake-transfer-engine,详见 u5-l5)在注册期把锁页 buffer 的 `(data_ptr, size)` 登记给 RDMA 传输引擎,远端 rank 之后凭地址直接读这块内存。共享池与它有两层交互:

1. **注册时机**:只在第一次使用共享池时注册。因为复用不改变地址,重复注册是冗余。
2. **注册命名**:张量名以 `memory_pool_{register_name}_{idx}` 生成,而共享使用者的 `register_name` 恒为 `__shared_memory_pool__`,**与 checkpoint 名无关**。这保证池的 p2p 身份在跨 checkpoint 复用时稳定,注销时也按同一个名字找到它。

#### 4.5.2 核心流程

```text
_register_parameters_to_p2p_store(name)
  ├─ pool = _get_memory_pool(name)                 # 共享使用者 → 拿到的是共享池
  ├─ register_name = name if name != 当前使用者 else "__shared_memory_pool__"
  └─ for idx, memory_buffer in enumerate(pool):
       named_tensors[f"memory_pool_{register_name}_{idx}"] = memory_buffer.buffer
       p2p_store.register_named_tensors(named_tensors)
```

#### 4.5.3 源码精读

命名规则与注册主体:

- [checkpoint_engine/ps.py:L721-L735](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L721-L735) `_register_parameters_to_p2p_store`:L727-L731 计算稳定注册名(当前共享使用者一律用 `__shared_memory_pool__`),L732-L735 逐桶以 `memory_pool_{register_name}_{idx}` 注册并记录 (ptr, size)。

只在首次注册的调用点(呼应 4.2.3):

- [checkpoint_engine/ps.py:L357-L358](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L357-L358) `if self._p2p_store is not None and _is_first_time: self._register_parameters_to_p2p_store(checkpoint_name)`。

注销侧使用同一套名字映射:

- [checkpoint_engine/ps.py:L737-L749](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L737-L749) `_unregister_parameters_from_p2p_store`:L742-L746 用相同规则算出 `unregister_name`,再按 `memory_pool_{unregister_name}_{idx}` 逐个注销。注意该函数只在 force 路径([ps.py:L402-L406](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L402-L406))会被共享使用者触达——让位路径在它之前就 return 了。

顺带一提,`update` / `gather_metas` 对共享池同样无感知,统一通过 `_get_memory_pool` 取池,例如拷贝数据到设备 buffer 时:

- [checkpoint_engine/ps.py:L705](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L705) `pool = self._get_memory_pool(checkpoint_name)[b.idx]`,按桶下标取 buffer。

#### 4.5.4 代码实践:写出注册名

1. **实践目标**:给定一段操作序列,写出 p2p store 中登记的张量名。
2. **操作步骤**:阅读 [checkpoint_engine/ps.py:L726-L735](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L726-L735),然后回答:(a) `register_checkpoint("ckpt_a", use_shared_memory_pool=True)` 且单桶时,p2p store 里的名字是什么?(b) 让位后 `register_checkpoint("ckpt_b", use_shared_memory_pool=True)` 会产生新名字吗?
3. **需要观察的现象 / 预期结果**:
   - (a) 因为 `ckpt_a` 是共享使用者,`register_name` 取 `__shared_memory_pool__`,名字为 `memory_pool___shared_memory_pool___0`(注意名字里连续三个下划线:格式一个 + 保留键前后各一个)。
   - (b) 不会。`ckpt_b` 同样是共享使用者,名字不变;而且由于 `_is_first_time` 为假,这次根本不会调用注册(见 L357-L358 的条件),远端继续沿用旧注册读同一块内存。
4. 本实践为源码阅读型,可直接从代码推出,无需运行。

#### 4.5.5 小练习与答案

**练习 1**:如果把命名规则改成直接用 checkpoint 名(即 `memory_pool_ckpt_a_0`),会出什么问题?

**答案**:两个问题。其一,跨代复用时每次都要以新名字重新注册、以旧名字注销,白白多做一次远端内存登记;其二,注销逻辑(让位路径不清注册)就找不到统一的名字——上一代留下的注册名是 `ckpt_a`,而当前要释放的是 `ckpt_b` 的池,名字对不上。稳定的 `__shared_memory_pool__` 命名把「池的身份」与「使用者的身份」解耦了。

**练习 2**:共享池复用时,远端 rank 是如何「读到新权重」的?中间没有任何通知吗?

**答案**:没有额外通知。RDMA 读的是注册过的物理地址,复用模式不换 buffer,新权重由 `register_tensor` 原地覆盖同一块内存。远端发起传输时读到的自然就是最新内容。这也解释了为什么 p2p 注册可以只在首次做——注册的是地址,不是数据。

**练习 3**:`gather_metas` 广播的元数据里,共享池的 ptr/size 与上一代相同吗?这对 join 复用模式(u6-l3)意味着什么?

**答案**:复用成功时相同——同一块内存,`_get_memory_pool` 返回的 buffer 的 `data_ptr()` 不变。这意味着一个新启动的推理实例即使拿到的是**上一代**导出的 metas,其中的地址依然有效(只要共享池没被 force 释放、没被改形状),这正是 join 复用模式能跨进程共享权重元数据的物理基础。

## 5. 综合实践

**任务**:写一个最小脚本,把本讲的四个机制点——首次定型、同形状复用、单一使用者拒绝、形状不匹配拒绝、force 释放后重新定型——全部走一遍,并用状态字段验证每一步。

下面是**示例代码**(不是项目原有代码,保存为 `shared_pool_demo.py` 在项目根目录运行;需要 GPU,因为 `pin_memory=True` 依赖 CUDA,且 `ParameterServer` 初始化需要设备):

```python
import os

import torch

from checkpoint_engine.ps import ParameterServer

os.environ["RANK"] = "0"
os.environ["WORLD_SIZE"] = "1"
os.environ["MASTER_ADDR"] = "localhost"
os.environ["MASTER_PORT"] = "25400"

ps = ParameterServer()
small = lambda: {"w": torch.randn(1024, 1024), "b": torch.randn(1024)}
big = lambda: {"w": torch.randn(2048, 2048), "b": torch.randn(2048)}


def state(tag):
    print(
        f"{tag:<28s} user={ps._current_shared_memory_pool_user!r:<28s} "
        f"pool_len={len(ps._memory_pool[ps.shared_memory_pool_name])}"
    )


ps.register_checkpoint("v1", named_tensors=small(), use_shared_memory_pool=True)
state("v1 首次注册(定型)")          # 池长度 0 -> 1

ps.unregister_checkpoint("v1")        # 非 force:让位
state("v1 让位")

ps.register_checkpoint("v2", named_tensors=small(), use_shared_memory_pool=True)
state("v2 同形状复用")                # 不分配,池长度不变

try:                                   # v2 仍是使用者
    ps.register_checkpoint("v3", named_tensors=big(), use_shared_memory_pool=True)
except AssertionError as e:
    print("单一使用者约束 ->", str(e).splitlines()[0])
state("v3 被拒绝后")

ps.unregister_checkpoint("v2")         # 让位,再试不同形状
try:
    ps.register_checkpoint("v3", named_tensors=big(), use_shared_memory_pool=True)
except AssertionError as e:
    print("形状约束 ->", str(e).splitlines()[0])
state("v3 再次被拒绝后")               # 池仍是旧形状,长度 1

# 陷阱演示:此刻 user 已是 "",直接 force 旧名字释放不了池
ps.unregister_checkpoint("v2", force=True)
state("让位后 force v2(无效)")        # 池仍在

ps.register_checkpoint("v2", named_tensors=small(), use_shared_memory_pool=True)
ps.unregister_checkpoint("v2", force=True)   # 趁使用者还在,真正释放
state("v2 force 释放")                 # 池长度回到 0

ps.register_checkpoint("v3", named_tensors=big(), use_shared_memory_pool=True)
state("v3 重新定型")                   # 按新形状首次分配
```

**操作步骤**:

1. 在有 GPU 的机器上,先跑通验收测试确认环境:`pytest tests/test_reuse_pin_memory.py -s -v`(`-s` 能看到测试里 `print("Caught expected AssertionError ...")` 的输出)。
2. 再运行上述脚本,对照打印的状态行。

**需要观察的现象与预期结果**:

- `v1 首次注册` 后 `pool_len` 从 0 变 1;`v2 同形状复用` 时日志仍会出现 `register pin_memory for bucket 1/1 ... start to copy tensors to buffer`(复用也走拷贝),但 `pool_len` 不变。
- 两次被拒分别命中 [ps.py:L341-L345](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L341-L345)(单一使用者)与 [pin_memory.py:L318-L321](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L318-L321)(逐桶 size 不等),异常消息文本可以区分两者。
- 「让位后 force」一行 `pool_len` 保持 1——印证 4.4 的陷阱;只有趁使用者持有时的 force 才让 `pool_len` 归 0。

**无 GPU 环境的替代实践(纯 CPU)**:完成 4.2.4 的状态推演表与 4.3.4 的手算布局,并用 `git show f69e116` 阅读 force 语义的原始提交,核对 4.4 节的三条返回点描述。脚本的实际运行输出**待本地验证**。

## 6. 本讲小结

- 共享池由三个状态支撑:保留键 `__shared_memory_pool__` 恒在 `_memory_pool` 中(空列表 = 尚未定型)、`_current_shared_memory_pool_user` 记录当前占用者(空串 = 无人使用)、`_get_memory_pool` 为下游提供无感知的三路读取。
- `register_checkpoint(use_shared_memory_pool=True)` 依次做单一使用者断言、首次判定、带旧池调用 `_register_checkpoint`(`inplace_pin` 强制关闭)、成功后登记使用者;赋值原子性 + 回滚走 not-found 路径,保证失败不伤池。
- 复用的真正执行点是 `_normal_pin_memory` 内层函数 `register_pin_memory`:`if shared_pin_memory:` 的真值分岔让「首次分配 / 后续复用」无需额外标志;约束是桶数量相同且逐桶字节数相等;metas 每轮重建,只有底层 buffer 张量被复用。
- `unregister_checkpoint` 对共享使用者有两副面孔:非 force 只清标记(让位,池与 p2p 注册保留),force 才删条目、重置空列表并 `host_empty_cache`(释放)。让位之后名字即失效,再 force 只会得到 not-found 警告——要释放必须趁使用者仍在时调用。
- 与 p2p store 的交互靠「稳定命名 + 仅首次注册」:共享池以 `__shared_memory_pool__` 为注册名,复用不改变 (ptr, size),远端按旧地址即可读到新权重。

## 7. 下一步学习建议

本讲结束后,数据结构与内存管理单元(u2)就完整了。建议下一步:

1. 进入 u3-l1(ParameterServer 初始化),看 `register_checkpoint` 所在的完整生命周期如何被 `__init__` 里的 TCPStore、设备识别等基础设施支撑。
2. 如果想先补齐内存侧的最后一块拼图,可回读 u2-l4 中 `unregister_checkpoint` 普通分支的手动 unpin 实现,理解 `manually_pinned` 标志为何在共享池路径永远不会被置 True。
3. 带着本讲的「地址不变 ⇒ 注册不变」结论去读 u5-l5(P2PStore 与 RDMA 设备发现),再读 u6-l3 的 join 复用模式,体会共享池与 metas 导出如何共同支撑「新实例零拷贝接入」。
