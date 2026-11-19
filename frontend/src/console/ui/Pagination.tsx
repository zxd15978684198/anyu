/** 分页条（W1-4）：服务端分页，页码与 total 由后端给，前端只负责跳转。 */
export function Pagination({
  page,
  pageSize,
  total,
  onChange,
  disabled,
}: {
  page: number;
  pageSize: number;
  total: number;
  onChange: (page: number) => void;
  disabled?: boolean;
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(page * pageSize, total);

  return (
    <div className="pagination">
      <span className="pagination-info">
        {total === 0 ? "共 0 条" : `第 ${from}–${to} 条，共 ${total} 条`}
      </span>
      <div className="pagination-buttons">
        <button
          type="button"
          className="btn-secondary btn-sm"
          onClick={() => onChange(page - 1)}
          disabled={disabled || page <= 1}
        >
          上一页
        </button>
        <span className="pagination-page">
          {page} / {pages}
        </span>
        <button
          type="button"
          className="btn-secondary btn-sm"
          onClick={() => onChange(page + 1)}
          disabled={disabled || page >= pages}
        >
          下一页
        </button>
      </div>
    </div>
  );
}
