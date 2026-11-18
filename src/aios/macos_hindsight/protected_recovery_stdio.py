"""Dedicated local administrator MCP for one protected recovery operation."""

import os
import re
import sys


def require_administrator():
    if os.getuid() != 0 or os.geteuid() != 0:
        raise PermissionError('macOS administrator identity required')


def serve(operation):
    require_administrator()
    # The caller's working directory can be protected by macOS privacy controls.
    os.chdir('/var/empty')
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations
    from ..hindsight_admin_recovery import RecoveryRefused

    server = MCPServer('sios-memory-administrator')

    @server.tool(
        description='Recover one approved unknown Hindsight write on the protected host.',
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                    idempotentHint=True, openWorldHint=False),
    )
    def memory_store_recover(request_id: str):
        try:
            return operation.run(request_id)
        except RecoveryRefused as error:
            code = str(error)
            if re.fullmatch(r'[a-z][a-z_]{1,80}', code):
                raise ToolError(code) from None
            raise ToolError('recovery_refused') from None
        except Exception:
            raise ToolError('protected_recovery_unavailable') from None

    server.run(transport='stdio')


def main():
    try:
        require_administrator()
        from .protected_recovery_authority import production_operation
        serve(production_operation())
    except PermissionError:
        print('administrator_recovery_access_denied', file=sys.stderr)
        return 1
    except Exception:
        print('administrator_recovery_startup_refused', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
