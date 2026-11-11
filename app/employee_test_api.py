"""Employee-number-only test endpoints, enabled through a separate application switch."""

import secrets
import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app import tickets
from app.attachments import UPLOAD_OPENAPI, read_uploads
from app.auth import Database, Principal, forbidden
from app.iam import IAMError, save_profile


router = APIRouter(prefix='/api/test/v1', tags=['员工工号测试接口'])
EmployeeNumber = Annotated[str, Query(min_length=1, max_length=64, pattern=r'^\S+$')]


def employee_principal(connection: sqlite3.Connection, employee_no: str) -> Principal:
    row = connection.execute(
        """SELECT u.* FROM users u JOIN iam_profiles p ON p.user_id = u.id
           WHERE p.employee_number = ?""", (employee_no,),
    ).fetchone()
    if row is None:
        raise IAMError('employee_not_linked', '工号尚未关联人员资料', 422)
    if not row['active']:
        raise IAMError('employee_inactive', '当前员工账号已停用', 403)
    if connection.execute(
        "SELECT 1 FROM user_roles WHERE user_id = ? AND role = 'submitter'", (row['id'],),
    ).fetchone() is None:
        raise forbidden()
    return Principal(
        id=row['id'], username=row['username'], display_name=row['display_name'],
        department=row['department'], email=row['email'], location=row['location'],
        roles=('submitter',), group_scopes={},
    )


def employee_user(request: Request, connection: Database, employee_no: EmployeeNumber):
    if not request.app.state.employee_test_api:
        raise HTTPException(status_code=404, detail={
            'code': 'test_api_disabled', 'message': '工号测试接口尚未开启',
        })
    mapping = connection.execute(
        'SELECT user_id FROM iam_profiles WHERE employee_number = ?', (employee_no,),
    ).fetchone()
    if mapping is None:
        client = request.app.state.iam_client
        if client is None:
            raise IAMError('iam_not_configured', '当前服务尚未配置 IAM 连接', 503)
        profile = client.lookup(username=employee_no)
        try:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                # Another request may have provisioned this employee during the lookup.
                if connection.execute(
                    'SELECT 1 FROM iam_profiles WHERE employee_number = ?', (employee_no,),
                ).fetchone() is None:
                    save_profile(connection, profile, password=secrets.token_urlsafe(32))
        except sqlite3.IntegrityError:
            raise IAMError('iam_identity_conflict', '工号与已有账号冲突，请联系管理员核实', 409) from None
    return employee_principal(connection, employee_no)


Employee = Annotated[Principal, Depends(employee_user)]


def employee_mutation(user: Employee, connection: Database, employee_no: EmployeeNumber):
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        current = employee_principal(connection, employee_no)
        if current.id != user.id:
            raise IAMError('iam_identity_conflict', '员工身份关联已变化，请重新调用', 409)
        yield current


EmployeeMutation = Annotated[Principal, Depends(employee_mutation, scope='function')]


@router.get('/service-systems')
def service_systems(user: Employee, connection: Database):
    rows = connection.execute(
        """SELECT s.id, s.name, s.group_id FROM service_systems s
           JOIN support_groups g ON g.id = s.group_id
           WHERE s.active = 1 AND g.active = 1 ORDER BY s.id""",
    ).fetchall()
    return {'items': [dict(row) for row in rows]}


@router.post('/conversations', status_code=201)
def create_conversation(body: tickets.ConversationRequest, user: EmployeeMutation, connection: Database):
    return tickets.create_conversation(body, user, connection)


@router.get('/conversations/{conversation_id}')
def get_conversation(conversation_id: str, user: Employee, connection: Database):
    return tickets.get_conversation(conversation_id, user, connection)


@router.post('/conversations/{conversation_id}/ticket', status_code=201)
def create_ticket(conversation_id: str, body: tickets.TicketRequest, response: Response,
                  user: EmployeeMutation, connection: Database):
    return tickets.create_ticket(conversation_id, body, response, user, connection)


@router.get('/tickets')
def list_tickets(
    user: Employee, connection: Database,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    q: Annotated[str | None, Query(max_length=200)] = None,
    status: Literal['pending_assignment', 'pending_acceptance', 'in_progress',
                    'awaiting_confirmation', 'closed', 'cancelled'] | None = None,
    service_system_id: Annotated[str | None, Query(max_length=128)] = None,
):
    return tickets.list_tickets(user, connection, limit, offset, q, status, service_system_id, scope='mine')


@router.get('/tickets/{ticket_id}')
def get_ticket(ticket_id: str, user: Employee, connection: Database):
    return tickets.get_ticket(ticket_id, user, connection, scope='mine')


@router.post('/tickets/{ticket_id}/supplements')
def add_supplement(ticket_id: str, body: tickets.SupplementRequest, user: EmployeeMutation, connection: Database):
    return tickets.add_supplement(ticket_id, body, user, connection)


@router.post('/tickets/{ticket_id}/confirmations')
def confirm_result(ticket_id: str, body: tickets.ConfirmationRequest, user: EmployeeMutation, connection: Database):
    return tickets.confirm_result(ticket_id, body, user, connection)


@router.post('/attachments', status_code=201, openapi_extra=UPLOAD_OPENAPI)
async def upload_attachments(request: Request, user: Employee, connection: Database, employee_no: EmployeeNumber):
    uploads = await read_uploads(request)
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        current = employee_principal(connection, employee_no)
        if current.id != user.id:
            raise IAMError('iam_identity_conflict', '员工身份关联已变化，请重新调用', 409)
        return {'items': tickets.save_uploads(connection, current, uploads)}


@router.get('/attachments/{file_id}')
def download_attachment(file_id: str, user: Employee, connection: Database):
    return tickets.download_attachment(file_id, user, connection)
