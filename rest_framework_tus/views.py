# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import json
import logging

from django.http import Http404
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from rest_framework import mixins, status
from rest_framework.exceptions import MethodNotAllowed
from rest_framework.metadata import BaseMetadata
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from rest_framework_tus.parsers import TusUploadStreamParser
from . import tus_api_version, tus_api_version_supported, tus_api_extensions, tus_api_checksum_algorithms, \
    settings as tus_settings, constants, signals, states
from .compat import reverse
from .exceptions import Conflict
from .models import get_upload_model
from .serializers import UploadSerializer
from .utils import encode_upload_metadata, checksum_matches

logger = logging.getLogger(__name__)


def has_required_tus_header(request):
    return hasattr(request, constants.TUS_RESUMABLE_FIELD_NAME)


def add_expiry_header(upload, headers):
    if upload.expires:
        headers['Upload-Expires'] = upload.expires.strftime('%a, %d %b %Y %H:%M:%S %Z')


class UploadMetadata(BaseMetadata):
    def determine_metadata(self, request, view):
        return {
            'Tus-Resumable': tus_api_version,
            'Tus-Version': ','.join(tus_api_version_supported),
            'Tus-Extension': ','.join(tus_api_extensions),
            'Tus-Max-Size': tus_settings.TUS_MAX_FILE_SIZE,
            'Tus-Checksum-Algorithm': ','.join(tus_api_checksum_algorithms),
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'PATCH,HEAD,GET,POST,OPTIONS',
            'Access-Control-Expose-Headers': 'Tus-Resumable,upload-length,upload-metadata,Location,Upload-Offset',
            'Access-Control-Allow-Headers':
                'Tus-Resumable,upload-length,upload-metadata,Location,Upload-Offset,content-type',
            'Cache-Control': 'no-store'
        }


class TusHeadMixin(object):
    def info(self, request, *args, **kwargs):
        # Validate tus header
        if not has_required_tus_header(request):
            return Response('Missing "{}" header.'.format('Tus-Resumable'), status=status.HTTP_400_BAD_REQUEST)

        try:
            upload = self.get_object()
        except Http404:
            # Instead of simply trowing a 404, we need to add a cache-control header to the response
            return Response('Not found.', headers={'Cache-Control': 'no-store'}, status=status.HTTP_404_NOT_FOUND)

        headers = {
            'Upload-Offset': upload.upload_offset,
            'Cache-Control': 'no-store'
        }

        if upload.upload_length >= 0:
            headers['Upload-Length'] = upload.upload_length

        if upload.upload_metadata:
            headers['Upload-Metadata'] = encode_upload_metadata(json.loads(upload.upload_metadata))

        # Add upload expiry to headers
        add_expiry_header(upload, headers)

        return Response(headers=headers, status=status.HTTP_200_OK)


class TusCreateMixin(mixins.CreateModelMixin):
    def create(self, request, *args, **kwargs):
        # Validate tus header
        if not has_required_tus_header(request):
            return Response('Missing "{}" header.'.format('Tus-Resumable'), status=status.HTTP_400_BAD_REQUEST)

        # Get file size from request
        upload_length = getattr(request, constants.UPLOAD_LENGTH_FIELD_NAME, -1)

        # Validate upload_length
        if upload_length > tus_settings.TUS_MAX_FILE_SIZE:
            return Response('Invalid "Upload-Length". Maximum value: {}.'.format(tus_settings.TUS_MAX_FILE_SIZE),
                            status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        # If upload_length is not given, we expect the defer header!
        if not upload_length or upload_length < 0:
            if getattr(request, constants.UPLOAD_DEFER_LENGTH_FIELD_NAME, -1) != 1:
                return Response('Missing "{Upload-Defer-Length}" header.', status=status.HTTP_400_BAD_REQUEST)

        # Get metadata from request
        upload_metadata = getattr(request, constants.UPLOAD_METADATA_FIELD_NAME, {})

        # Get data from metadata
        filename = upload_metadata.get('filename', '')

        # Retrieve serializer
        serializer = self.get_serializer(data={
            'upload_length': upload_length,
            'upload_metadata': json.dumps(upload_metadata),
            'filename': filename,
        })

        # Validate serializer
        serializer.is_valid(raise_exception=True)

        # Create upload object
        self.perform_create(serializer)

        # Get upload from serializer
        upload = serializer.instance

        # Prepare response headers
        headers = self.get_success_headers(serializer.data)

        # Maybe we're auto-expiring the upload...
        if tus_settings.TUS_UPLOAD_EXPIRES is not None:
            upload.expires = timezone.now() + tus_settings.TUS_UPLOAD_EXPIRES
            upload.save()

        # Add upload expiry to headers
        add_expiry_header(upload, headers)

        # By default, don't include a response body
        if not tus_settings.TUS_RESPONSE_BODY_ENABLED:
            return Response(headers=headers, status=status.HTTP_201_CREATED)

        return Response(serializer.data, headers=headers, status=status.HTTP_201_CREATED)

    def get_success_headers(self, data):
        try:
            return {'Location': reverse('rest_framework_tus:api:upload-detail', kwargs={'guid': data['guid']})}
        except (TypeError, KeyError):
            return {}


class TusPatchMixin(mixins.UpdateModelMixin):
    def get_chunk(self, request):
        if TusUploadStreamParser in self.parser_classes:
            return request.data['chunk']
        return request.body

    def update(self, request, *args, **kwargs):
        raise MethodNotAllowed

    def partial_update(self, request, *args, **kwargs):
        # Validate tus header
        if not has_required_tus_header(request):
            return Response('Missing "{}" header.'.format('Tus-Resumable'), status=status.HTTP_400_BAD_REQUEST)

        # Validate content type
        self.validate_content_type(request)

        # Retrieve object
        upload = self.get_object()

        # Get upload_offset
        upload_offset = getattr(request, constants.UPLOAD_OFFSET_NAME)

        # Validate upload_offset
        if upload_offset != upload.upload_offset:
            raise Conflict

        # Make sure there is a tempfile for the upload
        assert upload.get_or_create_temporary_file()

        # Change state
        if upload.state == states.INITIAL:
            upload.start_receiving()
            upload.save()

        # Write chunk
        chunk_bytes = self.get_chunk(request)

        # Check checksum  (http://tus.io/protocols/resumable-upload.html#checksum)
        upload_checksum = getattr(request, constants.UPLOAD_CHECKSUM_FIELD_NAME, None)
        if upload_checksum is not None:
            if upload_checksum[0] not in tus_api_checksum_algorithms:
                return Response('Unsupported Checksum Algorithm: {}.'.format(
                    upload_checksum[0]), status=status.HTTP_400_BAD_REQUEST)
            elif not checksum_matches(
                upload_checksum[0], upload_checksum[1], chunk_bytes):
                return Response('Checksum Mismatch.', status=460)

        # Write file
        chunk_size = int(request.META.get('CONTENT_LENGTH', 102400))
        try:
            upload.write_data(chunk_bytes, chunk_size)
        except Exception as e:
            return Response(str(e), status=status.HTTP_400_BAD_REQUEST)

        headers = {
            'Upload-Offset': upload.upload_offset,
        }

        if upload.upload_length == upload.upload_offset:
            # Trigger signal
            signals.received.send(sender=upload.__class__, instance=upload)

        # Add upload expiry to headers
        add_expiry_header(upload, headers)

        # By default, don't include a response body
        if not tus_settings.TUS_RESPONSE_BODY_ENABLED:
            return Response(headers=headers, status=status.HTTP_204_NO_CONTENT)

        # Create serializer
        serializer = self.get_serializer(instance=upload)

        return Response(serializer.data, headers=headers, status=status.HTTP_204_NO_CONTENT)

    @classmethod
    def validate_content_type(cls, request):
        content_type = request.META.get('headers', {}).get('Content-Type', '')

        if not content_type or content_type != TusUploadStreamParser.media_type:
            return Response(
                'Invalid value for "Content-Type" header: {}. Expected "{}".'
                    .format(content_type, TusUploadStreamParser.media_type), status=status.HTTP_400_BAD_REQUEST)


class TusTerminateMixin(mixins.DestroyModelMixin):
    def destroy(self, request, *args, **kwargs):
        # Retrieve object
        upload = self.get_object()

        # When the upload is still saving, we're not able to destroy the entity
        if upload.state == states.SAVING:
            return Response(_('Unable to terminate upload while in state "{}".'.format(upload.state)),
                            status=status.HTTP_409_CONFLICT)

        # Destroy object
        self.perform_destroy(upload)

        return Response(status=status.HTTP_204_NO_CONTENT)


class UploadViewSet(TusCreateMixin,
                    TusPatchMixin,
                    TusHeadMixin,
                    TusTerminateMixin,
                    GenericViewSet):
    serializer_class = UploadSerializer
    metadata_class = UploadMetadata
    lookup_field = 'guid'
    lookup_value_regex = '[a-zA-Z0-9]{8}-[a-zA-Z0-9]{4}-[a-zA-Z0-9]{4}-[a-zA-Z0-9]{4}-[a-zA-Z0-9]{12}'
    parser_classes = [TusUploadStreamParser]

    def get_queryset(self):
        return get_upload_model().objects.all()
