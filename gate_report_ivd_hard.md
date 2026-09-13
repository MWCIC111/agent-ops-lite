| 语料 | 配置 | 门控信号 | AUC | 正例 med | 负例 med |
|---|---|---|---|---|---|
| ivd | title×3 + content（线上现行） | ratio = top1/mean(top_k) | 0.404 | 1.05 | 1.06 |
| ivd | title×3 + content（线上现行） | gap = top1-top2 | 0.599 | 1.10 | 0.99 |
| ivd | title×3 + content（线上现行） | top1 绝对分 | 0.978 | 35.71 | 17.16 |
| ivd | title×3 + content（线上现行） | z = (top1-mean)/std | 0.515 | 1.49 | 1.49 |
