import astropy
from astropy.coordinates import EarthLocation
from astropy.time import Time
from astropy import units as u
from astroplan import (
    AirmassConstraint,
    AtNightConstraint,
    Observer,
    is_event_observable,
)
from datetime import datetime, timedelta
import geopandas
import healpy as hp
import humanize
import json
import jsonschema
import requests
from marshmallow.exceptions import ValidationError
import sqlalchemy as sa
from sqlalchemy.orm import joinedload, undefer
from sqlalchemy.orm import sessionmaker, scoped_session
import urllib
import numpy as np
import io
from tornado.ioloop import IOLoop
import functools
import ligo.skymap
from ligo.skymap.tool.ligo_skymap_plot_airmass import main as plot_airmass
from ligo.skymap import plot  # noqa: F401
import matplotlib
from matplotlib import dates
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import matplotlib.patches as mpatches
import pandas as pd
import simsurvey
from simsurvey.utils import model_tools
from simsurvey.models import AngularTimeSeriesSource
import tempfile
from ligo.skymap.distance import parameters_to_marginal_moments
from ligo.skymap.bayestar import rasterize
from ligo.skymap import plot  # noqa: F401 F811
import random
import afterglowpy
import sncosmo
import time

from baselayer.app.access import auth_or_token
from baselayer.app.env import load_env
from baselayer.app.flow import Flow
from baselayer.log import make_log
from baselayer.app.custom_exceptions import AccessError
from ..base import BaseHandler
from ...models import (
    Allocation,
    DefaultObservationPlanRequest,
    DBSession,
    EventObservationPlan,
    GcnEvent,
    Group,
    Instrument,
    InstrumentField,
    Localization,
    ObservationPlanRequest,
    PlannedObservation,
    SurveyEfficiencyForObservationPlan,
    SurveyEfficiencyForObservations,
    Telescope,
    User,
)
from ...models.schema import ObservationPlanPost
from ...utils.simsurvey import (
    get_simsurvey_parameters,
    random_parameters_notheta,
)

from skyportal.handlers.api.source import post_source
from skyportal.handlers.api.followup_request import post_assignment
from skyportal.handlers.api.observingrun import post_observing_run

env, cfg = load_env()
TREASUREMAP_URL = cfg['app.treasuremap_endpoint']

Session = scoped_session(sessionmaker(bind=DBSession.session_factory.kw["bind"]))

log = make_log('api/observation_plan')


def post_survey_efficiency_analysis(
    survey_efficiency_analysis, plan_id, user_id, session
):
    """Post survey efficiency analysis to database.

    Parameters
    ----------
    survey_efficiency_analysis: dict
        Dictionary describing survey efficiency analysis
    plan_id: int
        SkyPortal ID of Observation plan request
    user_id : int
        SkyPortal ID of User posting the GcnEvent
    session: sqlalchemy.Session
        Database session for this transaction
    """

    status_complete = False
    while not status_complete:
        observation_plan_request = (
            session.scalars(
                sa.select(ObservationPlanRequest)
                .options(
                    joinedload(ObservationPlanRequest.observation_plans)
                    .joinedload(EventObservationPlan.planned_observations)
                    .joinedload(PlannedObservation.field)
                )
                .where(ObservationPlanRequest.id == plan_id)
            )
            .unique()
            .all()
        )
        status_complete = observation_plan_request.status == "complete"

        if not status_complete:
            time.sleep(30)

    allocation = (
        session.scalars(
            sa.select(Allocation).where(
                Allocation.id == observation_plan_request.allocation_id
            )
        )
    ).first()
    localization = (
        session.scalars(
            sa.select(Localization).where(
                Localization.id == observation_plan_request.localization_id
            )
        )
    ).first()

    instrument = allocation.instrument

    observation_plan = observation_plan_request.observation_plans[0]
    planned_observations = observation_plan.planned_observations
    num_observations = len(observation_plan.planned_observations)
    if num_observations == 0:
        raise ValueError('Need at least one observation to evaluate efficiency')

    unique_filters = list(
        {
            planned_observation.filt
            for planned_observation in observation_plan.planned_observations
        }
    )

    if not set(unique_filters).issubset(set(instrument.sensitivity_data.keys())):
        raise ValueError('Need sensitivity_data for all filters present')

    for filt in unique_filters:
        if not {'exposure_time', 'limiting_magnitude', 'zeropoint'}.issubset(
            set(list(instrument.sensitivity_data[filt].keys()))
        ):
            raise ValueError(
                f'Sensitivity_data dictionary missing keys for filter {filt}'
            )

    payload = survey_efficiency_analysis["payload"]
    payload["optionalInjectionParameters"] = json.loads(
        payload["optionalInjectionParameters"]
    )
    payload["optionalInjectionParameters"] = get_simsurvey_parameters(
        payload["modelName"], payload["optionalInjectionParameters"]
    )

    survey_efficiency_analysis = SurveyEfficiencyForObservationPlan(
        requester_id=user_id,
        observation_plan_id=observation_plan.id,
        payload=payload,
        groups=observation_plan_request.target_groups,
        status='running',
    )
    session.add(survey_efficiency_analysis)
    session.commit()

    observations = []
    for ii, o in enumerate(planned_observations):
        obs_dict = o.to_dict()
        obs_dict['field'] = obs_dict['field'].to_dict()
        observations.append(obs_dict)

        if ii == 0:
            field = (
                session.query(InstrumentField)
                .options(undefer(InstrumentField.contour_summary))
                .get(obs_dict["field"]["id"])
            )
            if field is None:
                raise ValueError(
                    f'Missing field {obs_dict["field"]["id"]} required to estimate field size'
                )
            contour_summary = field.contour_summary["features"][0]
            coordinates = np.array(contour_summary["geometry"]["coordinates"])
            width = np.max(coordinates[:, 0]) - np.min(coordinates[:, 0])
            height = np.max(coordinates[:, 1]) - np.min(coordinates[:, 1])

    log(
        f'Simsurvey analysis in progress for ID {survey_efficiency_analysis.id}. Should be available soon.'
    )

    simsurvey_analysis = functools.partial(
        observation_simsurvey,
        observations,
        localization.id,
        instrument.id,
        survey_efficiency_analysis.id,
        "SurveyEfficiencyForObservationPlan",
        width=width,
        height=height,
        number_of_injections=payload['numberInjections'],
        number_of_detections=payload['numberDetections'],
        detection_threshold=payload['detectionThreshold'],
        minimum_phase=payload['minimumPhase'],
        maximum_phase=payload['maximumPhase'],
        model_name=payload['modelName'],
        optional_injection_parameters=payload['optionalInjectionParameters'],
    )

    IOLoop.current().run_in_executor(None, simsurvey_analysis)

    return survey_efficiency_analysis.id


def post_observation_plans(plans, user_id, session):
    """Post combined ObservationPlans to database.

    Parameters
    ----------
    plan: dict
        Observation plan dictionary
    user_id : int
        SkyPortal ID of User posting the GcnEvent
    session: sqlalchemy.Session
        Database session for this transaction
    """

    user = session.query(User).get(user_id)

    observation_plan_requests = []
    for plan in plans:

        try:
            data = ObservationPlanPost.load(plan)
        except ValidationError as e:
            raise ValidationError(
                f'Invalid / missing parameters: {e.normalized_messages()}'
            )

        data["requester_id"] = user.id
        data["last_modified_by_id"] = user.id
        data['allocation_id'] = int(data['allocation_id'])
        data['localization_id'] = int(data['localization_id'])

        allocation = session.scalars(
            Allocation.select(user).where(Allocation.id == data['allocation_id'])
        ).first()
        if allocation is None:
            raise AttributeError(
                f"Cannot access allocation with ID: {data['allocation_id']}"
            )

        instrument = allocation.instrument
        if instrument.api_classname_obsplan is None:
            raise AttributeError('Instrument has no remote API.')

        if not instrument.api_class_obsplan.implements()['submit']:
            raise AttributeError(
                'Cannot submit observation plan requests for this Instrument.'
            )

        target_groups = []
        for group_id in data.pop('target_group_ids', []):
            g = session.scalars(Group.select(user).where(Group.id == group_id)).first()
            if g is None:
                raise AttributeError(f"Cannot access group with ID: {group_id}")
            target_groups.append(g)

        try:
            formSchema = instrument.api_class_obsplan.custom_json_schema(
                instrument, user
            )
        except AttributeError:
            formSchema = instrument.api_class_obsplan.form_json_schema

        # validate the payload
        try:
            jsonschema.validate(data['payload'], formSchema)
        except jsonschema.exceptions.ValidationError as e:
            raise jsonschema.exceptions.ValidationError(
                f'Payload failed to validate: {e}'
            )

        observation_plan_request = ObservationPlanRequest.__schema__().load(data)
        observation_plan_request.target_groups = target_groups
        session.add(observation_plan_request)
        session.commit()

        observation_plan_requests.append(observation_plan_request)

    try:
        instrument.api_class_obsplan.submit_multiple(observation_plan_requests)
    except Exception as e:
        for observation_plan_request in observation_plan_requests:
            observation_plan_request.status = 'failed to submit'
        raise AttributeError(f'Error submitting observation plan: {e.args[0]}')
    finally:
        session.commit()

    flow = Flow()
    for observation_plan_request in observation_plan_requests:
        flow.push(
            '*',
            "skyportal/REFRESH_GCNEVENT",
            payload={"gcnEvent_dateobs": observation_plan_request.gcnevent.dateobs},
        )


def post_observation_plan(plan, user_id, session):
    """Post ObservationPlan to database.

    Parameters
    ----------
    plan: dict
        Observation plan dictionary
    user_id : int
        SkyPortal ID of User posting the GcnEvent
    session: sqlalchemy.Session
        Database session for this transaction
    """

    user = session.query(User).get(user_id)

    try:
        data = ObservationPlanPost.load(plan)
    except ValidationError as e:
        raise ValidationError(
            f'Invalid / missing parameters: {e.normalized_messages()}'
        )

    data["requester_id"] = user.id
    data["last_modified_by_id"] = user.id
    data['allocation_id'] = int(data['allocation_id'])
    data['localization_id'] = int(data['localization_id'])

    allocation = session.scalars(
        Allocation.select(user).where(Allocation.id == data['allocation_id'])
    ).first()
    if allocation is None:
        raise AttributeError(
            f"Cannot access allocation with ID: {data['allocation_id']}"
        )

    instrument = allocation.instrument
    if instrument.api_classname_obsplan is None:
        raise AttributeError('Instrument has no remote API.')

    if not instrument.api_class_obsplan.implements()['submit']:
        raise AttributeError(
            'Cannot submit observation plan requests for this Instrument.'
        )

    target_groups = []
    for group_id in data.pop('target_group_ids', []):
        g = session.scalars(Group.select(user).where(Group.id == group_id)).first()
        if g is None:
            raise AttributeError(f"Cannot access group with ID: {group_id}")
        target_groups.append(g)

    try:
        formSchema = instrument.api_class_obsplan.custom_json_schema(instrument, user)
    except AttributeError:
        formSchema = instrument.api_class_obsplan.form_json_schema

    # validate the payload
    try:
        jsonschema.validate(data['payload'], formSchema)
    except jsonschema.exceptions.ValidationError as e:
        raise jsonschema.exceptions.ValidationError(f'Payload failed to validate: {e}')

    observation_plan_request = ObservationPlanRequest.__schema__().load(data)
    observation_plan_request.target_groups = target_groups
    session.add(observation_plan_request)
    session.commit()

    flow = Flow()

    flow.push(
        '*',
        "skyportal/REFRESH_GCNEVENT",
        payload={"gcnEvent_dateobs": observation_plan_request.gcnevent.dateobs},
    )

    try:
        instrument.api_class_obsplan.submit(observation_plan_request)
    except Exception as e:
        observation_plan_request.status = 'failed to submit'
        raise AttributeError(f'Error submitting observation plan: {e.args[0]}')
    finally:
        session.commit()

    flow.push(
        '*',
        "skyportal/REFRESH_GCNEVENT",
        payload={"gcnEvent_dateobs": observation_plan_request.gcnevent.dateobs},
    )

    return observation_plan_request.id


class ObservationPlanRequestHandler(BaseHandler):
    @auth_or_token
    def post(self):
        """
        ---
        description: Submit observation plan request.
        tags:
          - observation_plan_requests
        requestBody:
          content:
            application/json:
              schema: ObservationPlanPost
        responses:
          200:
            content:
              application/json:
                schema:
                  allOf:
                    - $ref: '#/components/schemas/Success'
                    - type: object
                      properties:
                        data:
                          type: object
                          properties:
                            id:
                              type: integer
                              description: New observation plan request ID
        """
        json_data = self.get_json()
        if 'observation_plans' in json_data:
            observation_plans = json_data['observation_plans']
        else:
            observation_plans = [json_data]
        combine_plans = json_data.get('combine_plans', False)

        with self.Session() as session:
            ids = []
            if combine_plans:
                ids = post_observation_plans(
                    observation_plans, self.associated_user_object.id, session
                )
            else:
                for plan in observation_plans:
                    observation_plan_request_id = post_observation_plan(
                        plan, self.associated_user_object.id, session
                    )
                    ids = [observation_plan_request_id]

            return self.success(data={"ids": ids})

    @auth_or_token
    def get(self, observation_plan_request_id=None):
        """
        ---
        single:
          description: Get an observation plan.
          tags:
            - observation_plan_requests
          parameters:
            - in: path
              name: observation_plan_id
              required: true
              schema:
                type: string
            - in: query
              name: includePlannedObservations
              nullable: true
              schema:
                type: boolean
              description: |
                Boolean indicating whether to include associated planned observations. Defaults to false.
          responses:
            200:
              content:
                application/json:
                  schema: SingleObservationPlanRequest
            400:
              content:
                application/json:
                  schema: Error
        multiple:
          description: Get all observation plans.
          tags:
            - observation_plan_requests
          parameters:
            - in: query
              name: includePlannedObservations
              nullable: true
              schema:
                type: boolean
              description: |
                Boolean indicating whether to include associated planned observations. Defaults to false.
          responses:
            200:
              content:
                application/json:
                  schema: ArrayOfObservationPlanRequests
            400:
              content:
                application/json:
                  schema: Error
        """

        include_planned_observations = self.get_query_argument(
            "includePlannedObservations", False
        )
        if include_planned_observations:
            options = [
                joinedload(ObservationPlanRequest.observation_plans)
                .joinedload(EventObservationPlan.planned_observations)
                .joinedload(PlannedObservation.field),
                joinedload(ObservationPlanRequest.observation_plans).joinedload(
                    EventObservationPlan.statistics
                ),
            ]
        else:
            options = [
                joinedload(ObservationPlanRequest.observation_plans).joinedload(
                    EventObservationPlan.statistics
                )
            ]

        with self.Session() as session:
            if observation_plan_request_id is not None:
                observation_plan_request = session.scalars(
                    ObservationPlanRequest.select(
                        session.user_or_token, options=options
                    ).where(ObservationPlanRequest.id == observation_plan_request_id)
                ).first()
                if observation_plan_request is None:
                    return self.error(
                        f'Cannot find ObservationPlanRequest with ID: {observation_plan_request_id}'
                    )
                return self.success(data=observation_plan_request)

            observation_plan_requests = session.scalars(
                ObservationPlanRequest.select(session.user_or_token, options=options)
            ).all()
            return self.success(data=observation_plan_requests)

    @auth_or_token
    def delete(self, observation_plan_request_id):
        """
        ---
        description: Delete observation plan.
        tags:
          - observation_plan_requests
        parameters:
          - in: path
            name: observation_plan_id
            required: true
            schema:
              type: string
        responses:
          200:
            content:
              application/json:
                schema: Success
        """
        with self.Session() as session:
            observation_plan_request = session.scalars(
                ObservationPlanRequest.select(
                    session.user_or_token, mode="delete"
                ).where(ObservationPlanRequest.id == observation_plan_request_id)
            ).first()
            if observation_plan_request is None:
                return self.error(
                    f'Cannot find ObservationPlanRequest with ID: {observation_plan_request_id}'
                )

            dateobs = observation_plan_request.gcnevent.dateobs

            api = observation_plan_request.instrument.api_class_obsplan
            if not api.implements()['delete']:
                return self.error('Cannot delete observation plans on this instrument.')

            observation_plan_request.last_modified_by_id = (
                self.associated_user_object.id
            )
            api.delete(observation_plan_request.id)

            session.commit()

            self.push_all(
                action="skyportal/REFRESH_GCNEVENT",
                payload={"gcnEvent_dateobs": dateobs},
            )

            return self.success()


class ObservationPlanSubmitHandler(BaseHandler):
    @auth_or_token
    def post(self, observation_plan_request_id):
        """
        ---
        description: Submit an observation plan.
        tags:
          - observation_plan_requests
        parameters:
          - in: path
            name: observation_plan_id
            required: true
            schema:
              type: string
        responses:
          200:
            content:
              application/json:
                schema: SingleObservationPlanRequest
        """

        options = [
            joinedload(ObservationPlanRequest.observation_plans)
            .joinedload(EventObservationPlan.planned_observations)
            .joinedload(PlannedObservation.field)
        ]

        with self.Session() as session:
            observation_plan_request = session.scalars(
                ObservationPlanRequest.select(
                    session.user_or_token, options=options
                ).where(ObservationPlanRequest.id == observation_plan_request_id)
            ).first()
            if observation_plan_request is None:
                return self.error(
                    f'Cannot find ObservationPlanRequest with ID: {observation_plan_request_id}'
                )

            api = observation_plan_request.instrument.api_class_obsplan
            if not api.implements()['send']:
                return self.error('Cannot send observation plans on this instrument.')

            try:
                api.send(observation_plan_request)
            except Exception as e:
                observation_plan_request.status = 'failed to send'
                return self.error(
                    f'Error sending observation plan to telescope: {e.args[0]}'
                )
            finally:
                session.commit()
            self.push_all(
                action="skyportal/REFRESH_GCNEVENT",
                payload={"gcnEvent_dateobs": observation_plan_request.gcnevent.dateobs},
            )

            session.commit()

            return self.success(data=observation_plan_request)

    @auth_or_token
    def delete(self, observation_plan_request_id):
        """
        ---
        description: Remove an observation plan from the queue.
        tags:
          - observation_plan_requests
        parameters:
          - in: path
            name: observation_plan_id
            required: true
            schema:
              type: string
        responses:
          200:
            content:
              application/json:
                schema: Success
        """

        with self.Session() as session:
            observation_plan_request = session.scalars(
                ObservationPlanRequest.select(
                    session.user_or_token, mode="delete"
                ).where(ObservationPlanRequest.id == observation_plan_request_id)
            ).first()
            if observation_plan_request is None:
                return self.error(
                    f'Cannot find ObservationPlanRequest with ID: {observation_plan_request_id}'
                )

            api = observation_plan_request.instrument.api_class_obsplan
            if not api.implements()['remove']:
                return self.error(
                    'Cannot remove observation plans from the queue of this instrument.'
                )

            try:
                api.remove(observation_plan_request)
            except Exception as e:
                observation_plan_request.status = 'failed to remove from queue'
                return self.error(
                    f'Error removing observation plan from telescope: {e.args[0]}'
                )
            finally:
                session.commit()
            self.push_all(
                action="skyportal/REFRESH_GCNEVENT",
                payload={"gcnEvent_dateobs": observation_plan_request.gcnevent.dateobs},
            )

            session.commit()

            return self.success(data=observation_plan_request)


class ObservationPlanNameHandler(BaseHandler):
    @auth_or_token
    def get(self):
        """
        ---
        description: Get all Observation Plan names
        tags:
          - observation_plans
        responses:
          200:
            content:
              application/json:
                schema: Success
          400:
            content:
              application/json:
                schema: Error
        """

        with self.Session() as session:
            plan_names = (
                session.scalars(sa.select(EventObservationPlan.plan_name).distinct())
                .unique()
                .all()
            )
            return self.success(data=plan_names)


class ObservationPlanGCNHandler(BaseHandler):
    @auth_or_token
    def get(self, observation_plan_request_id):
        """
        ---
        description: Get a GCN-izable summary of the observation plan.
        tags:
          - observation_plan_requests
        parameters:
          - in: path
            name: observation_plan_id
            required: true
            schema:
              type: string
        responses:
          200:
            content:
              application/json:
                schema: SingleObservationPlanRequest
        """

        with self.Session() as session:

            stmt = (
                ObservationPlanRequest.select(session.user_or_token)
                .where(ObservationPlanRequest.id == observation_plan_request_id)
                .options(
                    joinedload(ObservationPlanRequest.observation_plans)
                    .joinedload(EventObservationPlan.planned_observations)
                    .joinedload(PlannedObservation.field)
                )
            )
            observation_plan_request = session.scalars(stmt).first()

            if observation_plan_request is None:
                return self.error(
                    f'Could not find observation_plan_request with ID {observation_plan_request_id}'
                )

            event = session.scalars(
                GcnEvent.select(
                    session.user_or_token, options=[joinedload(GcnEvent.gcn_notices)]
                ).where(GcnEvent.id == observation_plan_request.gcnevent_id)
            ).first()
            if event is None:
                return self.error(
                    message=f"Invalid GcnEvent ID: {observation_plan_request.gcnevent_id}"
                )

            stmt = Allocation.select(session.user_or_token).where(
                Allocation.id == observation_plan_request.allocation_id,
            )
            allocation = session.scalars(stmt).first()
            instrument = allocation.instrument

            observation_plan = observation_plan_request.observation_plans[0]
            statistics = observation_plan.statistics
            if len(statistics) == 0:
                return self.error('Need statistics computed to produce a GCN')
            statistics = statistics[0].statistics

            start_observation = Time(statistics["start_observation"], format='isot')
            num_observations = statistics["num_observations"]
            unique_filters = statistics["unique_filters"]
            total_time = statistics["total_time"]
            probability = statistics["probability"]
            area = statistics["area"]
            dt = statistics["dt"]

            trigger_time = Time(event.dateobs, format='datetime')

            content = f"""
            SUBJECT: Follow-up of {event.gcn_notices[0].stream} trigger {trigger_time.isot} with {instrument.name}.
            We observed the localization region of {event.gcn_notices[0].stream} trigger {trigger_time.isot} UTC with {instrument.name} on the {instrument.telescope.name}. We obtained a total of {num_observations} images covering {",".join(unique_filters)} bands for a total of {total_time} seconds. The observations covered {area:.1f} square degrees beginning at {start_observation.isot} ({humanize.naturaldelta(dt)} after the burst trigger time) corresponding to ~{int(100 * probability)}% of the probability enclosed in the localization region.
            """

            return self.success(data=content)


def observation_animations(
    observations,
    localization,
    output_format='gif',
    figsize=(10, 8),
    decay=4,
    alpha_default=1,
    alpha_cutoff=0.1,
):

    """Create a movie to display observations of a given skymap
    Parameters
    ----------
    observations : skyportal.models.observation_plan.PlannedObservation
        The planned observations associated with the request
    localization : skyportal.models.localization.Localization
        The skymap that the request is made based on
    output_format : str, optional
        "gif" or "mp4" -- determines the format of the returned movie
    figsize : tuple, optional
        Matplotlib figsize of the movie created
    decay: float, optional
        The alpha of older fields follows an exponential decay to
        avoid cluttering the screen, this is how fast it falls off
        set decay = 0 to have no exponential decay
    alpha_default: float, optional
        The alpha to assign all fields observed if decay is 0,
        unused otherwise
    alpha_cutoff: float, optional
        The alpha below which you don't draw a field since it is
        too light. Used to not draw lots of invisible fields and
        waste processing time.
    Returns
    -------
    dict
        success : bool
            Whether the request was successful or not, returning
            a sensible error in 'reason'
        name : str
            suggested filename based on `source_name` and `output_format`
        data : str
            binary encoded data for the movie (to be streamed)
        reason : str
            If not successful, a reason is returned.
    """

    surveyColors = {
        "ztfg": "#28A745",
        "ztfr": "#DC3545",
        "ztfi": "#F3DC11",
        "AllWISE": "#2F5492",
        "Gaia_DR3": "#FF7F0E",
        "PS1_DR1": "#3BBED5",
        "GALEX": "#6607C2",
        "TNS": "#ED6CF6",
    }

    filters = list({obs.filt for obs in observations})
    for filt in filters:
        if filt in surveyColors:
            continue
        surveyColors[filt] = "#" + ''.join(
            [random.choice('0123456789ABCDEF') for i in range(6)]
        )

    matplotlib.use("Agg")
    fig = plt.figure(figsize=figsize, constrained_layout=False)
    ax = plt.axes(projection='astro mollweide')
    ax.imshow_hpx(localization.flat_2d, cmap='cylon')

    old_artists = []

    def plot_schedule(k):
        for artist in old_artists:
            artist.remove()
        del old_artists[:]

        for i, obs in enumerate(observations):

            if decay != 0:
                alpha = np.exp((i - k) / decay)
            else:
                alpha = alpha_default
            if alpha > 1:
                alpha = 1

            if alpha > alpha_cutoff:
                coords = obs.field.contour_summary["features"][0]["geometry"][
                    "coordinates"
                ]
                ras = np.array(coords)[:, 0]
                # cannot handle 0-crossing well
                if len(np.where(ras > 180)[0]) > 0 and len(np.where(ras < 180)) > 0:
                    continue
                poly = plt.Polygon(
                    coords,
                    alpha=alpha,
                    facecolor=surveyColors[obs.filt],
                    edgecolor='black',
                    transform=ax.get_transform('world'),
                )
                ax.add_patch(poly)
                old_artists.append(poly)

        patches = []
        for filt in filters:
            patches.append(mpatches.Patch(color=surveyColors[filt], label=filt))
        plt.legend(handles=patches)

    if output_format == "gif":
        writer = animation.PillowWriter()
    elif output_format == "mp4":
        writer = animation.FFMpegWriter()
    else:
        raise ValueError('output_format must be gif or mp4')

    with tempfile.NamedTemporaryFile(mode='w', suffix='.' + output_format) as f:
        anim = animation.FuncAnimation(fig, plot_schedule, frames=len(observations))
        anim.save(f.name, writer=writer)
        f.flush()

        with open(f.name, mode='rb') as g:
            anim_content = g.read()

    return {
        "success": True,
        "name": f"{localization.localization_name}.{output_format}",
        "data": anim_content,
        "reason": "",
    }


class ObservationPlanMovieHandler(BaseHandler):
    @auth_or_token
    async def get(self, observation_plan_request_id):
        """
        ---
        description: Get a movie summary of the observation plan.
        tags:
          - observation_plan_requests
        parameters:
          - in: path
            name: observation_plan_id
            required: true
            schema:
              type: string
        responses:
          200:
            content:
              application/json:
                schema: SingleObservationPlanRequest
        """

        with self.Session() as session:

            stmt = (
                ObservationPlanRequest.select(session.user_or_token)
                .where(ObservationPlanRequest.id == observation_plan_request_id)
                .options(
                    joinedload(ObservationPlanRequest.observation_plans)
                    .joinedload(EventObservationPlan.planned_observations)
                    .joinedload(PlannedObservation.field)
                    .undefer(InstrumentField.contour_summary)
                )
            )
            observation_plan_request = session.scalars(stmt).first()

            if observation_plan_request is None:
                return self.error(
                    f'Could not find observation_plan_request with ID {observation_plan_request_id}'
                )

            localization = session.scalars(
                Localization.select(
                    session.user_or_token,
                ).where(Localization.id == observation_plan_request.localization_id)
            ).first()
            if localization is None:
                return self.error(
                    message=f"Invalid Localization dateobs: {observation_plan_request.localization_id}"
                )

            observation_plan = observation_plan_request.observation_plans[0]
            num_observations = len(observation_plan.planned_observations)
            if num_observations == 0:
                return self.error('Need at least one observation to produce a movie')

            observations = observation_plan.planned_observations

            output_format = 'gif'
            anim = functools.partial(
                observation_animations,
                observations,
                localization,
                output_format=output_format,
                figsize=(10, 8),
                decay=4,
                alpha_default=1,
                alpha_cutoff=0.1,
            )

            self.push_notification(
                'Movie generation in progress. Download will start soon.'
            )
            rez = await IOLoop.current().run_in_executor(None, anim)

            filename = rez["name"]
            data = io.BytesIO(rez["data"])

            await self.send_file(data, filename, output_type=output_format)


class ObservationPlanTreasureMapHandler(BaseHandler):
    @auth_or_token
    def post(self, observation_plan_request_id):
        """
        ---
        description: Submit the observation plan to treasuremap.space
        tags:
          - observation_plan_requests
        parameters:
          - in: path
            name: observation_plan_id
            required: true
            schema:
              type: string
        responses:
          200:
            content:
              application/json:
                schema: Success
          400:
            content:
              application/json:
                schema: Error
        """

        with self.Session() as session:

            stmt = (
                ObservationPlanRequest.select(session.user_or_token)
                .where(ObservationPlanRequest.id == observation_plan_request_id)
                .options(
                    joinedload(ObservationPlanRequest.observation_plans)
                    .joinedload(EventObservationPlan.planned_observations)
                    .joinedload(PlannedObservation.field)
                )
            )
            observation_plan_request = session.scalars(stmt).first()

            if observation_plan_request is None:
                return self.error(
                    f'Could not find observation_plan_request with ID {observation_plan_request_id}'
                )

            event = session.scalars(
                GcnEvent.select(
                    session.user_or_token, options=[joinedload(GcnEvent.gcn_notices)]
                ).where(GcnEvent.id == observation_plan_request.gcnevent_id)
            ).first()
            if event is None:
                return self.error(
                    message=f"Invalid GcnEvent ID: {observation_plan_request.gcnevent_id}"
                )

            stmt = Allocation.select(session.user_or_token).where(
                Allocation.id == observation_plan_request.allocation_id,
            )
            allocation = session.scalars(stmt).first()
            instrument = allocation.instrument

            altdata = allocation.altdata
            if not altdata:
                return self.error('Missing allocation information.')

            observation_plan = observation_plan_request.observation_plans[0]
            num_observations = observation_plan.num_observations
            if num_observations == 0:
                return self.error('Need at least one observation to produce a GCN')

            planned_observations = observation_plan.planned_observations

            graceid = event.graceid
            payload = {
                "graceid": graceid,
                "api_token": altdata['TREASUREMAP_API_TOKEN'],
            }

            pointings = []
            for obs in planned_observations:
                pointing = {}
                pointing["ra"] = obs.field.ra
                pointing["dec"] = obs.field.dec
                pointing["band"] = obs.filt
                pointing["instrumentid"] = str(instrument.treasuremap_id)
                pointing["status"] = "planned"
                pointing["time"] = Time(obs.obstime, format='datetime').isot
                pointing["depth"] = 0.0
                pointing["depth_unit"] = "ab_mag"
                pointings.append(pointing)
            payload["pointings"] = pointings

            url = urllib.parse.urljoin(TREASUREMAP_URL, 'api/v0/pointings')
            r = requests.post(url=url, json=payload)
            r.raise_for_status()
            request_json = r.json()
            errors = request_json["ERRORS"]
            if len(errors) > 0:
                return self.error(f'TreasureMap upload failed: {errors}')
            self.push_notification('TreasureMap upload succeeded')
            return self.success()

    @auth_or_token
    def delete(self, observation_plan_request_id):
        """
        ---
        description: Remove observation plan from treasuremap.space.
        tags:
          - observationplan_requests
        parameters:
          - in: path
            name: observation_plan_id
            required: true
            schema:
              type: string
        responses:
          200:
            content:
              application/json:
                schema: Success
        """

        with self.Session() as session:

            stmt = (
                ObservationPlanRequest.select(session.user_or_token)
                .where(ObservationPlanRequest.id == observation_plan_request_id)
                .options(
                    joinedload(ObservationPlanRequest.observation_plans)
                    .joinedload(EventObservationPlan.planned_observations)
                    .joinedload(PlannedObservation.field)
                )
            )
            observation_plan_request = session.scalars(stmt).first()

            if observation_plan_request is None:
                return self.error(
                    f'Could not find observation_plan_request with ID {observation_plan_request_id}'
                )

            event = session.scalars(
                GcnEvent.select(
                    session.user_or_token, options=[joinedload(GcnEvent.gcn_notices)]
                ).where(GcnEvent.id == observation_plan_request.gcnevent_id)
            ).first()
            if event is None:
                return self.error(
                    message=f"Invalid GcnEvent ID: {observation_plan_request.gcnevent_id}"
                )

            stmt = Allocation.select(session.user_or_token).where(
                Allocation.id == observation_plan_request.allocation_id,
            )
            allocation = session.scalars(stmt).first()
            instrument = allocation.instrument

            altdata = allocation.altdata
            if not altdata:
                return self.error('Missing allocation information.')

            graceid = event.graceid
            payload = {
                "graceid": graceid,
                "api_token": altdata['TREASUREMAP_API_TOKEN'],
                "instrumentid": instrument.treasuremap_id,
            }

            baseurl = urllib.parse.urljoin(TREASUREMAP_URL, 'api/v0/cancel_all')
            url = f"{baseurl}?{urllib.parse.urlencode(payload)}"
            r = requests.post(url=url)
            r.raise_for_status()
            request_text = r.text
            if "successfully" not in request_text:
                return self.error(f'TreasureMap delete failed: {request_text}')
            self.push_notification(f'TreasureMap delete succeeded: {request_text}.')
            return self.success()


class ObservationPlanSurveyEfficiencyHandler(BaseHandler):
    @auth_or_token
    def get(self, observation_plan_request_id):
        """
        ---
        description: Get survey efficiency analyses of the observation plan.
        tags:
          - observation_plan_requests
        parameters:
          - in: path
            name: observation_plan_id
            required: true
            schema:
              type: string
        responses:
          200:
            content:
              application/json:
                schema: ArrayOfSurveyEfficiencyForObservationPlans
        """

        with self.Session() as session:

            stmt = (
                ObservationPlanRequest.select(session.user_or_token)
                .where(ObservationPlanRequest.id == observation_plan_request_id)
                .options(
                    joinedload(ObservationPlanRequest.observation_plans).joinedload(
                        EventObservationPlan.survey_efficiency_analyses
                    )
                )
            )
            observation_plan_request = session.scalars(stmt).first()

            if observation_plan_request is None:
                return self.error(
                    f'Could not find observation_plan_request with ID {observation_plan_request_id}'
                )

            observation_plan = observation_plan_request.observation_plans[0]
            analysis_data = []
            for analysis in observation_plan.survey_efficiency_analyses:
                analysis_data.append(
                    {
                        **analysis.to_dict(),
                        'number_of_transients': analysis.number_of_transients,
                        'number_in_covered': analysis.number_in_covered,
                        'number_detected': analysis.number_detected,
                        'efficiency': analysis.efficiency,
                    }
                )

            return self.success(data=analysis_data)


class ObservationPlanGeoJSONHandler(BaseHandler):
    @auth_or_token
    def get(self, observation_plan_request_id):
        """
        ---
        description: Get GeoJSON summary of the observation plan.
        tags:
          - observation_plan_requests
        parameters:
          - in: path
            name: observation_plan_id
            required: true
            schema:
              type: string
        responses:
          200:
            content:
              application/json:
                schema: SingleObservationPlanRequest
        """

        with self.Session() as session:

            stmt = (
                ObservationPlanRequest.select(session.user_or_token)
                .where(ObservationPlanRequest.id == observation_plan_request_id)
                .options(
                    joinedload(ObservationPlanRequest.observation_plans)
                    .joinedload(EventObservationPlan.planned_observations)
                    .joinedload(PlannedObservation.field)
                    .undefer(InstrumentField.contour_summary)
                )
            )
            observation_plan_request = session.scalars(stmt).first()

            if observation_plan_request is None:
                return self.error(
                    f'Could not find observation_plan_request with ID {observation_plan_request_id}'
                )

            if len(observation_plan_request.observation_plans) == 0:
                return self.error(
                    f'Could not find an observation_plan associated with observation_plan_request ID {observation_plan_request_id}'
                )

            observation_plan = observation_plan_request.observation_plans[0]
            # features are JSON representations that the d3 stuff understands.
            # We use these to render the contours of the sky localization and
            # locations of the transients.

            geojson = []
            fields_in = []
            for observation in observation_plan.planned_observations:
                if observation.field_id not in fields_in:
                    fields_in.append(observation.field_id)
                    geojson.append(observation.field.contour_summary)
                else:
                    continue

            return self.success(data={'geojson': geojson})


class ObservationPlanFieldsHandler(BaseHandler):
    @auth_or_token
    def delete(self, observation_plan_request_id):
        """
        ---
        description: Delete selected fields from the observation plan.
        tags:
          - observation_plan_requests
        parameters:
          - in: path
            name: observation_plan_id
            required: true
            schema:
              type: string
        requestBody:
          content:
            application/json:
              schema:
                properties:
                  fieldIds:
                    type: array
                    items:
                      type: integer
                    description: List of field IDs to remove from the plan
        responses:
          200:
            content:
              application/json:
                schema: SingleObservationPlanRequest
        """

        data = self.get_json()
        field_ids_to_remove = data.pop('fieldIds', None)

        if field_ids_to_remove is None:
            return self.error('Need to specify field IDs to remove')

        with self.Session() as session:

            stmt = (
                ObservationPlanRequest.select(session.user_or_token)
                .where(ObservationPlanRequest.id == observation_plan_request_id)
                .options(
                    joinedload(ObservationPlanRequest.observation_plans)
                    .joinedload(EventObservationPlan.planned_observations)
                    .joinedload(PlannedObservation.field)
                    .undefer(InstrumentField.contour_summary)
                )
            )
            observation_plan_request = session.scalars(stmt).first()

            if observation_plan_request is None:
                return self.error(
                    f'Could not find observation_plan_request with ID {observation_plan_request_id}'
                )

            observation_plan = observation_plan_request.observation_plans[0]
            dateobs = observation_plan_request.gcnevent.dateobs

            for observation in observation_plan.planned_observations:
                if observation.field_id in field_ids_to_remove:
                    session.delete(observation)
                else:
                    continue
                session.commit()

            self.push_all(
                action="skyportal/REFRESH_GCNEVENT",
                payload={"gcnEvent_dateobs": dateobs},
            )

            return self.success()


class ObservationPlanWorldmapPlotHandler(BaseHandler):
    @auth_or_token
    async def get(self, localization_id):
        """
        ---
        description: Create a summary plot for the observability for a given event.
        tags:
          - localizations
        parameters:
          - in: path
            name: localization_id
            required: true
            schema:
              type: integer
            description: |
              ID of localization to generate map for
          - in: query
            name: maximumAirmass
            nullable: true
            schema:
              type: number
            description: |
              Maximum airmass to consider. Defaults to 2.5.
          - in: query
            name: twilight
            nullable: true
            schema:
              type: string
            description: |
                Twilight definition. Choices are astronomical (-18 degrees), nautical (-12 degrees), and civil (-6 degrees).
        responses:
          200:
            content:
              application/json:
                schema: Success
        """

        max_airmass = self.get_query_argument("maxAirmass", 2.5)
        twilight = self.get_query_argument("twilight", "astronomical")

        twilight_dict = {'astronomical': -18, 'nautical': -12, 'civil': -6}

        with self.Session() as session:

            stmt = Telescope.select(self.current_user)
            telescopes = session.scalars(stmt).all()

            stmt = Localization.select(self.current_user).where(
                Localization.id == localization_id
            )
            localization = session.scalars(stmt).first()
            m = localization.flat_2d
            nside = localization.nside
            npix = len(m)

            trigger_time = Time(localization.dateobs, format='datetime')

            # Look up (celestial) spherical polar coordinates of HEALPix grid.
            theta, phi = hp.pix2ang(nside, np.arange(npix))
            # Convert to RA, Dec.
            radecs = astropy.coordinates.SkyCoord(
                ra=phi * u.rad, dec=(0.5 * np.pi - theta) * u.rad
            )

            cmap = plt.cm.rainbow
            norm = matplotlib.colors.Normalize(vmin=0, vmax=1)
            sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
            sm.set_array([])

            colors = []
            for telescope in telescopes:
                if not telescope.fixed_location:
                    continue
                location = EarthLocation(
                    lon=telescope.lon * u.deg,
                    lat=telescope.lat * u.deg,
                    height=(telescope.elevation or 0) * u.m,
                )

                # Alt/az reference frame at observatory, now
                frame = astropy.coordinates.AltAz(
                    obstime=trigger_time, location=location
                )

                # Transform grid to alt/az coordinates at observatory, now
                altaz = radecs.transform_to(frame)

                # Where is the sun, now?
                sun_altaz = astropy.coordinates.get_sun(trigger_time).transform_to(
                    altaz
                )

                # How likely is it that the (true, unknown) location of the source
                # is within the area that is visible, now?
                prob = m[
                    (sun_altaz.alt <= twilight_dict[twilight] * u.deg)
                    & (altaz.secz <= max_airmass)
                ].sum()

                rgba_color = cmap(norm(prob))
                colors.append(rgba_color)

            world = geopandas.read_file(
                geopandas.datasets.get_path('naturalearth_lowres')
            )
            ds = [
                telescope.to_dict()
                for telescope in telescopes
                if telescope.fixed_location
            ]
            df = pd.DataFrame(ds)
            df['colors'] = colors
            gdf = geopandas.GeoDataFrame(
                df, geometry=geopandas.points_from_xy(df.lon, df.lat)
            )

            output_format = 'pdf'
            fig, (ax0, ax1) = plt.subplots(1, 2, figsize=(14, 10), width_ratios=[10, 1])
            world.plot(ax=ax0)
            gdf.plot(ax=ax0, color=gdf['colors'])
            scale = 5
            for idx, dat in gdf.iterrows():
                ax0.annotate(
                    dat.nickname,
                    (
                        dat.lon + scale * np.random.randn(),
                        dat.lat + scale * np.random.randn(),
                    ),
                )

            cbar = plt.colorbar(sm, cax=ax1, fraction=0.5)
            cbar.set_label(r'Observable Probability')

            buf = io.BytesIO()
            fig.savefig(buf, format=output_format, bbox_inches='tight')
            plt.close(fig)
            buf.seek(0)

            filename = f"worldmap.{output_format}"
            data = io.BytesIO(buf.read())

            await self.send_file(data, filename, output_type=output_format)


class ObservationPlanObservabilityPlotHandler(BaseHandler):
    @auth_or_token
    async def get(self, localization_id):
        """
        ---
        description: Create a summary plot for the observability for a given event.
        tags:
          - localizations
        parameters:
          - in: path
            name: localization_id
            required: true
            schema:
              type: integer
            description: |
              ID of localization to generate observability plot for
          - in: query
            name: maximumAirmass
            nullable: true
            schema:
              type: number
            description: |
              Maximum airmass to consider. Defaults to 2.5.
          - in: query
            name: twilight
            nullable: true
            schema:
              type: string
            description: |
                Twilight definition. Choices are astronomical (-18 degrees), nautical (-12 degrees), and civil (-6 degrees).
        responses:
          200:
            content:
              application/json:
                schema: Success
        """

        max_airmass = self.get_query_argument("maxAirmass", 2.5)
        twilight = self.get_query_argument("twilight", "astronomical")

        with self.Session() as session:

            stmt = Telescope.select(self.current_user)
            telescopes = session.scalars(stmt).all()

            stmt = Localization.select(self.current_user).where(
                Localization.id == localization_id
            )
            localization = session.scalars(stmt).first()
            cent = localization.contour['features'][0]['geometry']['coordinates']
            coords = astropy.coordinates.SkyCoord(cent[0], cent[1], unit='deg')

            trigger_time = Time(localization.dateobs, format='datetime')
            times = trigger_time + np.linspace(0, 1) * u.day

            observers = []
            for telescope in telescopes:
                if not telescope.fixed_location:
                    continue
                location = EarthLocation(
                    lon=telescope.lon * u.deg,
                    lat=telescope.lat * u.deg,
                    height=(telescope.elevation or 0) * u.m,
                )

                observers.append(Observer(location, name=telescope.nickname))
            observers = list(reversed(observers))

            constraints = [
                getattr(AtNightConstraint, f'twilight_{twilight}')(),
                AirmassConstraint(max_airmass),
            ]

            output_format = 'pdf'
            fig = plt.figure(figsize=(14, 10))
            width, height = fig.get_size_inches()
            fig.set_size_inches(width, (len(observers) + 1) / 16 * width)
            ax = plt.axes()
            locator = dates.AutoDateLocator()
            formatter = dates.DateFormatter('%H:%M')
            ax.set_xlim([times[0].plot_date, times[-1].plot_date])
            ax.xaxis.set_major_formatter(formatter)
            ax.xaxis.set_major_locator(locator)
            ax.set_xlabel(f"Time from {min(times).datetime.date()} [UTC]")
            plt.setp(ax.get_xticklabels(), rotation=30, ha='right')
            ax.set_yticks(np.arange(len(observers)))
            ax.set_yticklabels([observer.name for observer in observers])
            ax.yaxis.set_tick_params(left=False)
            ax.grid(axis='x')
            ax.spines['bottom'].set_visible(False)
            ax.spines['top'].set_visible(False)

            for i, observer in enumerate(observers):
                observable = 100 * np.dot(
                    1.0, is_event_observable(constraints, observer, coords, times)
                )
                ax.contourf(
                    times.plot_date,
                    [i - 0.4, i + 0.4],
                    np.tile(observable, (2, 1)),
                    levels=np.arange(10, 110, 10),
                    cmap=plt.get_cmap().reversed(),
                )

            buf = io.BytesIO()
            fig.savefig(buf, format=output_format, bbox_inches='tight')
            plt.close(fig)
            buf.seek(0)

            filename = f"observability.{output_format}"
            data = io.BytesIO(buf.read())

            await self.send_file(data, filename, output_type=output_format)


class ObservationPlanAirmassChartHandler(BaseHandler):
    @auth_or_token
    async def get(self, localization_id, telescope_id):
        """
        ---
        description: Get an airmass chart for the GcnEvent
        tags:
          - observation_plan_requests
        parameters:
          - in: path
            name: localization_id
            required: true
            schema:
              type: integer
            description: |
              ID of localization to generate airmass chart for
          - in: path
            name: telescope_id
            required: true
            schema:
              type: integer
            description: |
              ID of telescope to generate airmass chart for
        responses:
          200:
            content:
              application/json:
                schema: Success
        """

        with self.Session() as session:

            stmt = Telescope.select(self.current_user).where(
                Telescope.id == telescope_id
            )
            telescope = session.scalars(stmt).first()

            stmt = Localization.select(self.current_user).where(
                Localization.id == localization_id
            )
            localization = session.scalars(stmt).first()

            trigger_time = astropy.time.Time(localization.dateobs, format='datetime')

            output_format = 'pdf'
            with tempfile.NamedTemporaryFile(
                suffix='.fits'
            ) as fitsfile, tempfile.NamedTemporaryFile(
                suffix=f'.{output_format}'
            ) as imgfile, matplotlib.style.context(
                'default'
            ):
                ligo.skymap.io.write_sky_map(
                    fitsfile.name, localization.table_2d, moc=True
                )
                plot_airmass(
                    [
                        '--site-longitude',
                        str(telescope.lon),
                        '--site-latitude',
                        str(telescope.lat),
                        '--site-height',
                        str(telescope.elevation),
                        '--time',
                        trigger_time.isot,
                        fitsfile.name,
                        '-o',
                        imgfile.name,
                    ]
                )

                with open(imgfile.name, mode='rb') as g:
                    content = g.read()

            data = io.BytesIO(content)
            filename = (
                f"{localization.localization_name}-{telescope.nickname}.{output_format}"
            )

            await self.send_file(data, filename, output_type=output_format)


class ObservationPlanCreateObservingRunHandler(BaseHandler):
    @auth_or_token
    def post(self, observation_plan_request_id):
        """
        ---
        description: Submit the fields in the observation plan
           to an observing run
        tags:
          - observation_plan_requests
        parameters:
          - in: path
            name: observation_plan_id
            required: true
            schema:
              type: integer
            description: |
              ID of observation plan request to create observing run for
        responses:
          200:
            content:
              application/json:
                schema: Success
          400:
            content:
              application/json:
                schema: Error
        """

        data = self.get_json()

        with self.Session() as session:
            observation_plan_request = session.scalars(
                ObservationPlanRequest.select(
                    session.user_or_token,
                    options=[
                        joinedload(ObservationPlanRequest.observation_plans)
                        .joinedload(EventObservationPlan.planned_observations)
                        .joinedload(PlannedObservation.field)
                    ],
                ).where(ObservationPlanRequest.id == observation_plan_request_id),
            ).first()
            if observation_plan_request is None:
                raise self.error(
                    f'Cannot access ObservationPlanRequest with ID {observation_plan_request_id}'
                )

            allocation = session.scalars(
                Allocation.select(session.user_or_token).where(
                    Allocation.id == observation_plan_request.allocation_id
                )
            ).first()

            if allocation is None:
                raise self.error(
                    f'Cannot find Allocation with ID {observation_plan_request.allocation_id}'
                )

            instrument = allocation.instrument

            observation_plan = observation_plan_request.observation_plans[0]
            planned_observations = observation_plan.planned_observations

            if len(planned_observations) == 0:
                return self.error('Cannot create observing run with no observations.')

            observing_run = {
                'instrument_id': instrument.id,
                'group_id': allocation.group_id,
                'calendar_date': str(observation_plan.validity_window_end.date()),
            }
            run_id = post_observing_run(
                observing_run, self.associated_user_object.id, session
            )

            min_priority = 1
            max_priority = 5
            priorities = []
            for obs in planned_observations:
                priorities.append(obs.weight)

            for obs in planned_observations:
                source = {
                    'id': f'{obs.field.ra}-{obs.field.dec}',
                    'ra': obs.field.ra,
                    'dec': obs.field.dec,
                }
                if 'groupIds' in data and len(data['groupIds']) > 0:
                    source['group_ids'] = data['groupIds']
                else:
                    source['group_ids'] = [allocation.group_id]

                obj_id = post_source(source, self.associated_user_object.id, session)
                if np.max(priorities) - np.min(priorities) == 0.0:
                    # assign equal weights if all the same
                    normalized_priority = 0.5
                else:
                    normalized_priority = (obs.weight - np.min(priorities)) / (
                        np.max(priorities) - np.min(priorities)
                    )

                priority = np.ceil(
                    (max_priority - min_priority) * normalized_priority + min_priority
                )
                assignment = {
                    'run_id': run_id,
                    'obj_id': obj_id,
                    'priority': str(int(priority)),
                }
                try:
                    post_assignment(assignment, session)
                except ValueError:
                    # No need to assign multiple times to same run
                    pass

            self.push_notification('Observing run post succeeded')
            return self.success()


def observation_simsurvey(
    observations,
    localization_id,
    instrument_id,
    survey_efficiency_analysis_id,
    survey_efficiency_analysis_type,
    width,
    height,
    number_of_injections=1000,
    number_of_detections=2,
    detection_threshold=5,
    minimum_phase=0,
    maximum_phase=3,
    model_name='kilonova',
    optional_injection_parameters={},
):

    """Perform the simsurvey analyis for a given skymap
    Parameters
    ----------
    observations : skyportal.models.observation_plan.PlannedObservation
        The planned observations associated with the request
    localization_id : int
        The id of the skyportal.models.localization.Localization that the request is made based on
    instrument_id : int
        The id of the skyportal.models.instrument.Instrument that the request is made based on
    survey_efficiency_analysis_id : int
        The id of the survey efficiency analysis for the request (either skyportal.models.survey_efficiency.SurveyEfficiencyForObservations or skyportal.models.survey_efficiency.SurveyEfficiencyForObservationPlan).
    survey_efficiency_analysis_type : str
        Either SurveyEfficiencyForObservations or SurveyEfficiencyForObservationPlan.
    width : float
        Width of the telescope field of view in degrees.
    height : float
        Height of the telescope field of view in degrees.
    number_of_injections : int
        Number of simulations to evaluate efficiency with. Defaults to 1000.
    number_of_detections : int
        Number of detections required for detection. Defaults to 1.
    detection_threshold : int
        Threshold (in sigmas) required for detection. Defaults to 5.
    minimum_phase : int
        Minimum phase (in days) post event time to consider detections. Defaults to 0.
    maximum_phase : int
        Maximum phase (in days) post event time to consider detections. Defaults to 3.
    model_name : str
        Model to simulate efficiency for. Must be one of kilonova, afterglow, or linear. Defaults to kilonova.
    optional_injection_parameters: dict
        Optional parameters to specify the injection type, along with a list of possible values (to be used in a dropdown UI)
    """

    session = Session()

    try:

        localization = session.scalars(
            sa.select(Localization).where(Localization.id == localization_id)
        ).first()
        if localization is None:
            raise ValueError(f'No localization with ID {localization_id}')

        instrument = session.scalars(
            sa.select(Instrument).where(Instrument.id == instrument_id)
        ).first()
        if instrument is None:
            raise ValueError(f'No instrument with ID {instrument_id}')

        if survey_efficiency_analysis_type == "SurveyEfficiencyForObservations":
            survey_efficiency_analysis = session.scalars(
                sa.select(SurveyEfficiencyForObservations).where(
                    SurveyEfficiencyForObservations.id == survey_efficiency_analysis_id
                )
            ).first()
            if survey_efficiency_analysis is None:
                raise ValueError(
                    f'No SurveyEfficiencyForObservations with ID {survey_efficiency_analysis_id}'
                )
        elif survey_efficiency_analysis_type == "SurveyEfficiencyForObservationPlan":
            survey_efficiency_analysis = session.scalars(
                sa.select(SurveyEfficiencyForObservationPlan).where(
                    SurveyEfficiencyForObservationPlan.id
                    == survey_efficiency_analysis_id
                )
            ).first()
            if survey_efficiency_analysis is None:
                raise ValueError(
                    f'No SurveyEfficiencyForObservationPlan with ID {survey_efficiency_analysis_id}'
                )
        else:
            raise ValueError(
                'survey_efficiency_analysis_type must be SurveyEfficiencyForObservations or SurveyEfficiencyForObservationPlan'
            )

        trigger_time = astropy.time.Time(localization.dateobs, format='datetime')

        keys = ['ra', 'dec', 'field_id', 'limMag', 'jd', 'filter', 'skynoise']
        pointings = {k: [] for k in keys}
        for obs in observations:
            nmag = -2.5 * np.log10(
                np.sqrt(
                    instrument.sensitivity_data[obs["filt"]]['exposure_time']
                    / obs["exposure_time"]
                )
            )

            if "limmag" in obs:
                limMag = obs["limmag"]
            else:
                limMag = (
                    instrument.sensitivity_data[obs["filt"]]['limiting_magnitude']
                    + nmag
                )
            zp = instrument.sensitivity_data[obs["filt"]]['zeropoint'] + nmag

            pointings["ra"].append(obs["field"]["ra"])
            pointings["dec"].append(obs["field"]["dec"])
            pointings["filter"].append(obs["filt"])
            pointings["jd"].append(Time(obs["obstime"], format='datetime').jd)
            pointings["field_id"].append(obs["field"]["field_id"])

            pointings["limMag"].append(limMag)
            pointings["skynoise"].append(10 ** (-0.4 * (limMag - zp)) / 5.0)
            pointings["zp"] = zp

        df = pd.DataFrame.from_dict(pointings)
        plan = simsurvey.SurveyPlan(
            time=df['jd'],
            band=df['filter'],
            obs_field=df['field_id'].astype(int),
            skynoise=df['skynoise'],
            zp=df['zp'],
            width=width,
            height=height,
            fields={
                k: v for k, v in pointings.items() if k in ['ra', 'dec', 'field_id']
            },
        )

        order = hp.nside2order(localization.nside)
        t = rasterize(localization.table, order)

        if 'DISTMU' in t:
            result = t['PROB'], t['DISTMU'], t['DISTSIGMA'], t['DISTNORM']
            hp_data = hp.reorder(result, 'NESTED', 'RING')
            map_struct = {}
            map_struct['prob'] = hp_data[0]
            map_struct['distmu'] = hp_data[1]
            map_struct['distsigma'] = hp_data[2]

            distmean, diststd = parameters_to_marginal_moments(
                map_struct['prob'], map_struct['distmu'], map_struct['distsigma']
            )

            distance_lower = astropy.coordinates.Distance(
                np.max([1, (distmean - 5 * diststd)]) * u.Mpc
            )
            distance_upper = astropy.coordinates.Distance(
                np.max([2, (distmean + 5 * diststd)]) * u.Mpc
            )
        else:
            result = t['PROB']
            hp_data = hp.reorder(result, 'NESTED', 'RING')
            map_struct = {}
            map_struct['prob'] = hp_data
            distance_lower = astropy.coordinates.Distance(1 * u.Mpc)
            distance_upper = astropy.coordinates.Distance(1000 * u.Mpc)

        if model_name == "kilonova":
            phase, wave, cos_theta, flux = model_tools.read_possis_file(
                optional_injection_parameters["injection_filename"]
            )
            transientprop = {
                'lcmodel': sncosmo.Model(
                    AngularTimeSeriesSource(
                        phase=phase, wave=wave, flux=flux, cos_theta=cos_theta
                    )
                )
            }
            template = "AngularTimeSeriesSource"

        elif model_name == "afterglow":
            phases = np.linspace(
                optional_injection_parameters["t_i"],
                optional_injection_parameters["t_f"],
                optional_injection_parameters["ntime"],
            )
            wave = np.linspace(
                optional_injection_parameters["lambda_min"],
                optional_injection_parameters["lambda_max"],
                optional_injection_parameters["nlambda"],
            )
            nu = 3e8 / (wave * 1e-10)

            grb_param_keys = [
                "jetType",
                "specType",
                "thetaObs",
                "E0",
                "thetaCore",
                "thetaWing",
                "n0",
                "p",
                "epsilon_e",
                "epsilon_B",
                "z",
                "d_L",
                "xi_N",
            ]
            grb_params = {k: optional_injection_parameters[k] for k in grb_param_keys}
            # explicitly case E0 and d_L as float as they like to be an int
            grb_params["E0"] = float(grb_params["E0"])
            grb_params["d_L"] = float(grb_params["d_L"])

            flux = []
            for phase in phases:
                t = phase * np.ones(nu.shape)
                mJys = afterglowpy.fluxDensity(t, nu, **grb_params)
                Jys = 1e-3 * mJys
                # convert to erg/s/cm^2/A
                flux.append(Jys * 2.99792458e-05 / (wave**2))
            transientprop = {
                'lcmodel': sncosmo.Model(
                    sncosmo.TimeSeriesSource(phases, wave, np.array(flux))
                ),
                'lcsimul_func': random_parameters_notheta,
            }
            template = None

        elif model_name == "linear":
            phases = np.linspace(
                optional_injection_parameters["t_i"],
                optional_injection_parameters["t_f"],
                optional_injection_parameters["ntime"],
            )
            wave = np.linspace(
                optional_injection_parameters["lambda_min"],
                optional_injection_parameters["lambda_max"],
                optional_injection_parameters["nlambda"],
            )
            magdiff = (
                optional_injection_parameters["mag"]
                + phases * optional_injection_parameters["dmag"]
            )
            F_Lxlambda2 = 10 ** (-(magdiff + 2.406) / 2.5)
            waves, F_Lxlambda2s = np.meshgrid(wave, F_Lxlambda2)
            flux = F_Lxlambda2s / (waves) ** 2
            transientprop = {
                'lcmodel': sncosmo.Model(sncosmo.TimeSeriesSource(phases, wave, flux)),
                'lcsimul_func': random_parameters_notheta,
            }
            template = None

        tr = simsurvey.get_transient_generator(
            [distance_lower.z, distance_upper.z],
            transient="generic",
            template=template,
            ntransient=number_of_injections,
            ratefunc=lambda z: 5e-7,
            dec_range=(-90, 90),
            ra_range=(0, 360),
            mjd_range=(trigger_time.jd, trigger_time.jd),
            transientprop=transientprop,
            skymap=map_struct,
        )

        survey = simsurvey.SimulSurvey(
            generator=tr,
            plan=plan,
            phase_range=(minimum_phase, maximum_phase),
            n_det=number_of_detections,
            threshold=detection_threshold,
        )

        lcs = survey.get_lightcurves(notebook=True)

        data = {
            'lcs': lcs._properties["lcs"],
            'meta': lcs._properties["meta"],
            'meta_rejected': lcs._properties["meta_rejected"],
            'meta_notobserved': lcs._properties["meta_notobserved"],
            'stats': lcs._derived_properties["stats"],
            'side': lcs._side_properties,
        }

        class NumpyEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, np.ndarray):
                    return obj.tolist()
                return json.JSONEncoder.default(self, obj)

        survey_efficiency_analysis.lightcurves = json.dumps(data, cls=NumpyEncoder)
        survey_efficiency_analysis.status = 'complete'

        session.merge(survey_efficiency_analysis)
        session.commit()

        return log(
            f"Finished survey efficiency analysis for ID {survey_efficiency_analysis.id}"
        )

    except Exception as e:
        return log(
            f"Unable to complete survey efficiency analysis {survey_efficiency_analysis.id}: {e}"
        )
    finally:
        Session.remove()


def observation_simsurvey_plot(
    lcs,
    output_format='pdf',
    figsize=(10, 8),
):

    """Perform the simsurvey analyis for a given skymap
    Parameters
    ----------
    lcs : simsurvey.simulsurvey.LightcurveCollection
        A collection of light curves for efficiency assessment
    output_format : str, optional
        "pdf" or "png" -- determines the format of the returned plot
    figsize : tuple, optional
        Matplotlib figsize of the movie created

    Returns
    -------
    dict
        success : bool
            Whether the request was successful or not, returning
            a sensible error in 'reason'
        name : str
            suggested filename based on `source_name` and `output_format`
        data : str
            binary encoded data for the plot (to be streamed)
        reason : str
            If not successful, a reason is returned.
    """

    matplotlib.use("Agg")
    fig = plt.figure(figsize=figsize, constrained_layout=False)
    ax = plt.axes([0.05, 0.05, 0.9, 0.9], projection='geo degrees mollweide')
    ax.grid()
    if lcs['meta_notobserved'] is not None:
        ax.scatter(
            lcs['meta_notobserved']['ra'],
            lcs['meta_notobserved']['dec'],
            transform=ax.get_transform('world'),
            marker='*',
            color='grey',
            label='not_observed',
            alpha=0.7,
        )
    if lcs['meta'] is not None:
        ax.scatter(
            lcs['meta']['ra'],
            lcs['meta']['dec'],
            transform=ax.get_transform('world'),
            marker='*',
            color='red',
            label='detected',
            alpha=0.7,
        )
    ax.legend(loc=0)
    ax.set_ylabel('Declination [deg]')
    ax.set_xlabel('RA [deg]')

    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format=output_format)
    plt.close(fig)
    buf.seek(0)

    return {
        "success": True,
        "name": f"simsurvey.{output_format}",
        "data": buf.read(),
        "reason": "",
    }


class ObservationPlanSimSurveyHandler(BaseHandler):
    @auth_or_token
    async def get(self, observation_plan_request_id):
        """
        ---
        description: Perform an efficiency analysis of the observation plan.
        tags:
          - observation_plan_requests
        parameters:
          - in: path
            name: observation_plan_id
            required: true
            schema:
              type: string
          - in: query
            name: numberInjections
            nullable: true
            schema:
              type: number
            description: |
              Number of simulations to evaluate efficiency with. Defaults to 1000.
          - in: query
            name: numberDetections
            nullable: true
            schema:
              type: number
            description: |
              Number of detections required for detection. Defaults to 1.
          - in: query
            name: detectionThreshold
            nullable: true
            schema:
              type: number
            description: |
              Threshold (in sigmas) required for detection. Defaults to 5.
          - in: query
            name: minimumPhase
            nullable: true
            schema:
              type: number
            description: |
              Minimum phase (in days) post event time to consider detections. Defaults to 0.
          - in: query
            name: maximumPhase
            nullable: true
            schema:
              type: number
            description: |
              Maximum phase (in days) post event time to consider detections. Defaults to 3.
          - in: query
            name: model_name
            nullable: true
            schema:
              type: string
            description: |
              Model to simulate efficiency for. Must be one of kilonova, afterglow, or linear. Defaults to kilonova.
          - in: query
            name: optionalInjectionParameters
            type: object
            additionalProperties:
              type: array
              items:
                type: string
                description: |
                  Optional parameters to specify the injection type, along
                  with a list of possible values (to be used in a dropdown UI)
          - in: query
            name: group_ids
            nullable: true
            schema:
              type: array
              items:
                type: integer
              description: |
                List of group IDs corresponding to which groups should be
                able to view the analyses. Defaults to all of requesting user's
                groups.
        responses:
          200:
            content:
              application/json:
                schema: Success
        """

        number_of_injections = int(self.get_query_argument("numberInjections", 1000))
        number_of_detections = int(self.get_query_argument("numberDetections", 1))
        detection_threshold = float(self.get_query_argument("detectionThreshold", 5))
        minimum_phase = float(self.get_query_argument("minimumPhase", 0))
        maximum_phase = float(self.get_query_argument("maximumPhase", 3))
        model_name = self.get_query_argument("modelName", "kilonova")
        optional_injection_parameters = json.loads(
            self.get_query_argument("optionalInjectionParameters", '{}')
        )

        if model_name not in ["kilonova", "afterglow", "linear"]:
            return self.error(
                f"{model_name} must be one of kilonova, afterglow or linear"
            )

        optional_injection_parameters = get_simsurvey_parameters(
            model_name, optional_injection_parameters
        )

        group_ids = self.get_query_argument('group_ids', None)
        with self.Session() as session:

            if not group_ids:
                group_ids = [
                    g.id for g in self.associated_user_object.accessible_groups
                ]

            try:
                stmt = Group.select(self.current_user).where(Group.id.in_(group_ids))
                groups = session.scalars(stmt).all()
            except AccessError:
                return self.error('Could not find any accessible groups.', status=403)

            stmt = (
                ObservationPlanRequest.select(session.user_or_token)
                .where(ObservationPlanRequest.id == observation_plan_request_id)
                .options(
                    joinedload(ObservationPlanRequest.observation_plans)
                    .joinedload(EventObservationPlan.planned_observations)
                    .joinedload(PlannedObservation.field)
                )
            )
            observation_plan_request = session.scalars(stmt).first()

            if observation_plan_request is None:
                return self.error(
                    f'Could not find observation_plan_request with ID {observation_plan_request_id}'
                )

            stmt = Allocation.select(session.user_or_token).where(
                Allocation.id == observation_plan_request.allocation_id,
            )
            allocation = session.scalars(stmt).first()

            stmt = Localization.select(self.current_user).where(
                Localization.id == observation_plan_request.localization_id
            )
            localization = session.scalars(stmt).first()
            if localization is None:
                return self.error(
                    f'Localization with ID {observation_plan_request.localization_id} inaccessible.'
                )

            instrument = allocation.instrument

            if instrument.sensitivity_data is None:
                return self.error('Need sensitivity_data to evaluate efficiency')

            observation_plan = observation_plan_request.observation_plans[0]
            planned_observations = observation_plan.planned_observations
            num_observations = len(observation_plan.planned_observations)
            if num_observations == 0:
                self.push_notification(
                    'Need at least one observation to evaluate efficiency',
                    notification_type='error',
                )
                return self.error(
                    'Need at least one observation to evaluate efficiency'
                )

            unique_filters = list(
                {
                    planned_observation.filt
                    for planned_observation in observation_plan.planned_observations
                }
            )

            if not set(unique_filters).issubset(
                set(instrument.sensitivity_data.keys())
            ):
                return self.error('Need sensitivity_data for all filters present')

            for filt in unique_filters:
                if not {'exposure_time', 'limiting_magnitude', 'zeropoint'}.issubset(
                    set(list(instrument.sensitivity_data[filt].keys()))
                ):
                    return self.error(
                        f'Sensitivity_data dictionary missing keys for filter {filt}'
                    )

            payload = {
                'number_of_injections': number_of_injections,
                'number_of_detections': number_of_detections,
                'detection_threshold': detection_threshold,
                'minimum_phase': minimum_phase,
                'maximum_phase': maximum_phase,
                'model_name': model_name,
                'optional_injection_parameters': optional_injection_parameters,
            }

            survey_efficiency_analysis = SurveyEfficiencyForObservationPlan(
                requester_id=self.associated_user_object.id,
                observation_plan_id=observation_plan.id,
                payload=payload,
                groups=groups,
                status='running',
            )
            session.add(survey_efficiency_analysis)
            session.commit()

            observations = []
            for ii, o in enumerate(planned_observations):
                obs_dict = o.to_dict()
                obs_dict['field'] = obs_dict['field'].to_dict()
                observations.append(obs_dict)

                if ii == 0:
                    stmt = (
                        InstrumentField.select(self.current_user)
                        .where(InstrumentField.id == obs_dict["field"]["id"])
                        .options(undefer(InstrumentField.contour_summary))
                    )
                    field = session.scalars(stmt).first()
                    if field is None:
                        return self.error(
                            message=f'Missing field {obs_dict["field"]["id"]} required to estimate field size'
                        )
                    contour_summary = field.to_dict()["contour_summary"]["features"][0]
                    coordinates = np.array(contour_summary["geometry"]["coordinates"])
                    width = np.max(coordinates[:, 0]) - np.min(coordinates[:, 0])
                    height = np.max(coordinates[:, 1]) - np.min(coordinates[:, 1])

            self.push_notification(
                f'Simsurvey analysis in progress for ID {survey_efficiency_analysis.id}. Should be available soon.'
            )
            simsurvey_analysis = functools.partial(
                observation_simsurvey,
                observations,
                localization.id,
                instrument.id,
                survey_efficiency_analysis.id,
                "SurveyEfficiencyForObservationPlan",
                width=width,
                height=height,
                number_of_injections=payload['number_of_injections'],
                number_of_detections=payload['number_of_detections'],
                detection_threshold=payload['detection_threshold'],
                minimum_phase=payload['minimum_phase'],
                maximum_phase=payload['maximum_phase'],
                model_name=payload['model_name'],
                optional_injection_parameters=payload['optional_injection_parameters'],
            )

            IOLoop.current().run_in_executor(None, simsurvey_analysis)

            return self.success(data={"id": survey_efficiency_analysis.id})


class ObservationPlanSimSurveyPlotHandler(BaseHandler):
    @auth_or_token
    async def get(self, survey_efficiency_analysis_id):
        """
        ---
        description: Create a summary plot for a simsurvey efficiency calculation.
        tags:
          - survey_efficiency_for_observations
        parameters:
          - in: path
            name: survey_efficiency_analysis_id
            required: true
            schema:
              type: string
        responses:
          200:
            content:
              application/json:
                schema: Success
        """

        with self.Session() as session:
            survey_efficiency_analysis = session.scalars(
                SurveyEfficiencyForObservationPlan.select(session.user_or_token).where(
                    SurveyEfficiencyForObservationPlan.id
                    == survey_efficiency_analysis_id
                )
            ).first()
            if survey_efficiency_analysis is None:
                return self.error(
                    f'Cannot access survey_efficiency_analysis for id {survey_efficiency_analysis_id}'
                )

            if survey_efficiency_analysis.lightcurves is None:
                return self.error(
                    f'survey_efficiency_analysis for id {survey_efficiency_analysis_id} not complete'
                )

            output_format = 'pdf'
            simsurvey_analysis = functools.partial(
                observation_simsurvey_plot,
                lcs=json.loads(survey_efficiency_analysis.lightcurves),
                output_format=output_format,
            )

            self.push_notification(
                'Simsurvey analysis in progress. Should be available soon.'
            )
            rez = await IOLoop.current().run_in_executor(None, simsurvey_analysis)

            filename = rez["name"]
            data = io.BytesIO(rez["data"])

            await self.send_file(data, filename, output_type=output_format)


class DefaultObservationPlanRequestHandler(BaseHandler):
    @auth_or_token
    def post(self):
        """
        ---
        description: Create default observation plan requests.
        tags:
          - default_observation_plan
        requestBody:
          content:
            application/json:
              schema: DefaultObservationPlanPost
        responses:
          200:
            content:
              application/json:
                schema:
                  allOf:
                    - $ref: '#/components/schemas/Success'
                    - type: object
                      properties:
                        data:
                          type: object
                          properties:
                            id:
                              type: integer
                              description: New default observation plan request ID
        """
        data = self.get_json()

        with self.Session() as session:
            target_group_ids = data.pop('target_group_ids', [])
            stmt = Group.select(self.current_user).where(Group.id.in_(target_group_ids))
            target_groups = session.scalars(stmt).all()

            stmt = Allocation.select(session.user_or_token).where(
                Allocation.id == data['allocation_id'],
            )
            allocation = session.scalars(stmt).first()
            if allocation is None:
                return self.error(
                    f"Cannot access allocation with ID: {data['allocation_id']}",
                    status=403,
                )

            instrument = allocation.instrument
            if instrument.api_classname_obsplan is None:
                return self.error('Instrument has no remote API.', status=403)

            try:
                formSchema = instrument.api_class_obsplan.custom_json_schema(
                    instrument, self.current_user
                )
            except AttributeError:
                formSchema = instrument.api_class_obsplan.form_json_schema

            payload = data['payload']
            if "start_date" in payload:
                return self.error('Cannot have start_date in the payload')
            else:
                payload['start_date'] = str(datetime.utcnow())

            if "end_date" in payload:
                return self.error('Cannot have end_date in the payload')
            else:
                payload['end_date'] = str(datetime.utcnow() + timedelta(days=1))

            if "queue_name" in payload:
                return self.error('Cannot have queue_name in the payload')
            else:
                payload['queue_name'] = "ToO_{str(datetime.utcnow()).replace(' ','T')}"

            # validate the payload
            try:
                jsonschema.validate(payload, formSchema)
            except jsonschema.exceptions.ValidationError as e:
                return self.error(f'Payload failed to validate: {e}', status=403)

            default_observation_plan_request = (
                DefaultObservationPlanRequest.__schema__().load(data)
            )
            default_observation_plan_request.target_groups = target_groups

            session.add(default_observation_plan_request)
            session.commit()

            self.push_all(action="skyportal/REFRESH_DEFAULT_OBSERVATION_PLANS")
            return self.success(data={"id": default_observation_plan_request.id})

    @auth_or_token
    def get(self, default_observation_plan_id=None):
        """
        ---
        single:
          description: Retrieve a single default observation plan
          tags:
            - default_observation_plans
          parameters:
            - in: path
              name: default_observation_plan_id
              required: true
              schema:
                type: integer
          responses:
            200:
              content:
                application/json:
                  schema: SingleDefaultObservationPlanRequest
            400:
              content:
                application/json:
                  schema: Error
        multiple:
          description: Retrieve all default observation plans
          tags:
            - filters
          responses:
            200:
              content:
                application/json:
                  schema: ArrayOfDefaultObservationPlanRequests
            400:
              content:
                application/json:
                  schema: Error
        """

        with self.Session() as session:
            if default_observation_plan_id is not None:
                default_observation_plan_request = session.scalars(
                    DefaultObservationPlanRequest.select(
                        session.user_or_token,
                        options=[joinedload(DefaultObservationPlanRequest.allocation)],
                    ).where(
                        DefaultObservationPlanRequest.id == default_observation_plan_id
                    )
                ).first()
                if default_observation_plan_request is None:
                    return self.error(
                        f'Cannot find DefaultObservationPlanRequest with ID {default_observation_plan_id}'
                    )
                return self.success(data=default_observation_plan_request)

            default_observation_plan_requests = (
                session.scalars(
                    DefaultObservationPlanRequest.select(
                        session.user_or_token,
                        options=[joinedload(DefaultObservationPlanRequest.allocation)],
                    )
                )
                .unique()
                .all()
            )

            default_observation_plan_data = []
            for request in default_observation_plan_requests:
                default_observation_plan_data.append(
                    {
                        **request.to_dict(),
                        'allocation': request.allocation.to_dict(),
                    }
                )

            return self.success(data=default_observation_plan_data)

    @auth_or_token
    def delete(self, default_observation_plan_id):
        """
        ---
        description: Delete a default observation plan
        tags:
          - filters
        parameters:
          - in: path
            name: default_observation_plan_id
            required: true
            schema:
              type: integer
        responses:
          200:
            content:
              application/json:
                schema: Success
        """

        with self.Session() as session:

            stmt = DefaultObservationPlanRequest.select(session.user_or_token).where(
                DefaultObservationPlanRequest.id == default_observation_plan_id
            )
            default_observation_plan_request = session.scalars(stmt).first()

            if default_observation_plan_request is None:
                return self.error(
                    'Default observation plan with ID {default_observation_plan_id} is not available.'
                )

            session.delete(default_observation_plan_request)
            session.commit()
            self.push_all(action="skyportal/REFRESH_DEFAULT_OBSERVATION_PLANS")
            return self.success()
