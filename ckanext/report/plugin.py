import flask

import ckan.plugins as p
from ckan.lib.render import TemplateNotFound
from ckanext.report.interfaces import IReport
from ckanext.report.controllers import ReportController,\
    make_csv_from_dicts, ensure_data_is_dicts, anonymise_user_names

import ckanext.report.helpers as helpers
import ckanext.report.logic.action.get as action_get
import ckanext.report.logic.action.update as action_update
import ckanext.report.logic.auth.get as auth_get
import ckanext.report.logic.auth.update as auth_update
from ckanext.report.report_registry import Report


log = __import__('logging').getLogger(__name__)

t = p.toolkit
c = t.c

# flask report view
def report_view(report_name=None, organization=None):
    refresh = False
    if not report_name:
        try:
            reports = t.get_action('report_list')({}, {})
        except t.NotAuthorized:
            t.abort(401)
        return t.render('report/index.html', extra_vars={'reports': reports})
    else:
        try:
            report = t.get_action('report_show')({}, {'id': report_name})
        except t.NotAuthorized:
            t.abort(401)
        except t.ObjectNotFound:
            t.abort(404)
    if organization and 'organization' not in report['option_defaults']:
        t.redirect_to(helpers.relative_url_for(organization=None))
    elif organization and 'organization' in report['option_defaults'] and \
            report['option_defaults']['organization']:
        org = report['option_defaults']['organization']
        t.redirect_to(helpers.relative_url_for(organization=org))

    options = Report.add_defaults_to_options(t.request.params, report['option_defaults'])
    if 'format' in options:
        format = options.pop('format')
    else:
        format = None
    if 'organization' in report['option_defaults']:
        options['organization'] = organization
    options_html = {}
    c.options = options  # for legacy genshi snippets
    for option in options:
        if option not in report['option_defaults']:
            # e.g. 'refresh' param
            log.warn('Not displaying report option HTML for param %s as option not recognized')
            continue
        option_display_params = {'value': options[option],
                                 'default': report['option_defaults'][option]}
        try:
            options_html[option] = \
                t.render_snippet('report/option_%s.html' % option,
                                 data=option_display_params)
        except TemplateNotFound:
            log.warn('Not displaying report option HTML for param %s as no template found')
            continue

    if t.request.method == 'POST' and not format:
        refresh = True

    if refresh:
        try:
            t.get_action('report_refresh')({}, {'id': report_name, 'options': options})
        except t.NotAuthorized:
            t.abort(401)
        # Don't want the refresh=1 in the url once it is done
        t.redirect_to(helpers.relative_url_for(refresh=None))

    # Check for any options not allowed by the report
    for key in options:
        if key not in report['option_defaults']:
            t.abort(400, 'Option not allowed by report: %s' % key)

    try:
        data, report_date = t.get_action('report_data_get')({}, {'id': report_name, 'options': options})
    except t.ObjectNotFound:
        t.abort(404)
    except t.NotAuthorized:
        t.abort(401)

    if format and format != 'html':
        ensure_data_is_dicts(data)
        anonymise_user_names(data, organization=options.get('organization'))
        if format == 'csv':
            try:
                key = t.get_action('report_key_get')({}, {'id': report_name, 'options': options})
            except t.NotAuthorized:
                t.abort(401)
            filename = 'report_%s.csv' % key
            t.response.headers['Content-Type'] = 'application/csv'
            t.response.headers['Content-Disposition'] = str('attachment; filename=%s' % (filename))
            return make_csv_from_dicts(data['table'])
        elif format == 'json':
            t.response.headers['Content-Type'] = 'application/json'
            data['generated_at'] = report_date
            return json.dumps(data)
        else:
            t.abort(400, 'Format not known - try html, json or csv')

    are_some_results = bool(data['table'] if 'table' in data
                            else data)
    # A couple of context variables for legacy genshi reports
    c.data = data
    c.options = options
    return t.render('report/view.html', extra_vars={
        'report': report, 'report_name': report_name, 'data': data,
        'report_date': report_date, 'options': options,
        'options_html': options_html,
        'report_template': report['template'],
        'are_some_results': are_some_results})


class ReportPlugin(p.SingletonPlugin):
    p.implements(p.IConfigurer)
    p.implements(p.ITemplateHelpers)
    p.implements(p.IActions, inherit=True)
    p.implements(p.IAuthFunctions, inherit=True)
    if p.toolkit.check_ckan_version(min_version='2.8.0'):
        p.implements(p.IBlueprint)
    else:
        p.implements(p.IRoutes, inherit=True)  

    # IRoutes

    def before_map(self, map):
        report_ctlr = 'ckanext.report.controllers:ReportController'
        map.connect('reports', '/report', controller=report_ctlr,
                    action='index')
        map.redirect('/reports', '/report')
        map.connect('report', '/report/:report_name', controller=report_ctlr,
                    action='view')
        map.connect('report-org', '/report/:report_name/:organization',
                    controller=report_ctlr, action='view')
        return map

    # IBlueprints

    def get_blueprint(self):
        report_ctrl = ReportController()
        blueprint = flask.Blueprint(self.name, self.__module__)
        blueprint.template_folder = 'templates'
        rules = [
            ('/report', 'reports', report_view()),
            ('/report/<report_name>', 'report', report_view(report_name)),
            ('/report/<report_name>/<organization>',
             'report-org', report_view(report_name, organization))
        ]
        for rule in rules:
            blueprint.add_url_rule(*rule)

        return blueprint

    # IConfigurer

    def update_config(self, config):
        p.toolkit.add_template_directory(config, 'templates')

    # ITemplateHelpers

    def get_helpers(self):
        from ckanext.report import helpers as h
        return {
            'report__relative_url_for': h.relative_url_for,
            'report__chunks': h.chunks,
            'report__organization_list': h.organization_list,
            'report__render_datetime': h.render_datetime,
            'report__explicit_default_options': h.explicit_default_options,
            }

    # IActions
    def get_actions(self):
        return {'report_list': action_get.report_list,
                'report_show': action_get.report_show,
                'report_data_get': action_get.report_data_get,
                'report_key_get': action_get.report_key_get,
                'report_refresh': action_update.report_refresh}

    # IAuthFunctions
    def get_auth_functions(self):
        return {'report_list': auth_get.report_list,
                'report_show': auth_get.report_show,
                'report_data_get': auth_get.report_data_get,
                'report_key_get': auth_get.report_key_get,
                'report_refresh': auth_update.report_refresh}


class TaglessReportPlugin(p.SingletonPlugin):
    '''
    This is a working example only. To be kept simple and demonstrate features,
    rather than be particularly meaningful.
    '''
    p.implements(IReport)

    # IReport

    def register_reports(self):
        from . import reports
        return [reports.tagless_report_info]

